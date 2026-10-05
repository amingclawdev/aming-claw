"""Opt-in, receipt-bound APFS duplicate maintenance. No scheduler or deletion.

Calls enter through stale_artifact_cleanup under its SQLite/build/cleanup locks.
All paths are service-derived; clients name a plan or opaque operation, never
an archive path. Completion proves bytes/metadata, not physical block savings.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
import time
import math
import selectors
from functools import wraps
from time import monotonic as _metadata_clock
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from . import graph_snapshot_store as snapshots
from . import project_service

DIMENSION = "graph_snapshot_duplicates"
REVISION = 1
PAIRS = (("semantic-enrichment/rounds/round-000/semantic-graph.json",
          "semantic-enrichment/semantic-graph.json"),
         ("semantic-enrichment/rounds/round-000/semantic-index.json",
          "semantic-enrichment/semantic-index.json"))
TERMINAL = frozenset({"complete", "completed", "closed", "done", "failed", "cancelled",
                      "abandoned", "superseded", "merged", "fixed", "resolved", "revoked",
                      "expired", "succeeded", "success", "ok", "candidate_ready", "terminalized_stale"})
PRESERVED = ("mode", "uid", "gid", "mtime_ns", "flags", "xattrs", "acl")
MAX_LIVE_ROWS = 2000
MAX_JSON_BYTES = 1024 * 1024
CLONE_NOFOLLOW_ANY = 0x8

_METADATA_HELPER = Path(__file__).with_name("snapshot_cow_metadata.py")
_METADATA_SECONDS = 2.0
_METADATA_CONNECTION: ContextVar[Any] = ContextVar("cow_metadata_connection", default=None)
_METADATA_UNREAPED: dict[int, Any] = {}


class CowRefusal(ValueError):
    def __init__(self, reason: str, metadata: dict[str, Any] | None = None):
        super().__init__(reason)
        self.metadata = metadata or {}


class CowPrejournalRefusal(CowRefusal):
    """Native preflight exited before creating any journal/backup/stage."""
    pass


@dataclass(frozen=True)
class RunSelection:
    """Internal immutable narrowing; never accepted from a remote request."""
    snapshot_ids: tuple[str, ...]
    max_snapshots: int
    max_pairs: int
    max_hash_bytes: int

    def budgets(self, base: dict[str, int]) -> dict[str, int]:
        requested = {"max_snapshots": self.max_snapshots, "max_pairs": self.max_pairs,
                     "max_hash_bytes": self.max_hash_bytes}
        if (not isinstance(self.snapshot_ids, tuple) or not self.snapshot_ids
                or len(set(self.snapshot_ids)) != len(self.snapshot_ids)
                or any(not isinstance(sid, str) or not snapshots._snapshot_id_is_component(sid)
                       for sid in self.snapshot_ids)
                or any(type(v) is not int or not 1 <= v <= base[k] for k, v in requested.items())
                or len(self.snapshot_ids) > self.max_snapshots or self.max_pairs < 2 * len(self.snapshot_ids)):
            raise CowRefusal("cow_run_selection_invalid")
        return requested

    def receipt(self) -> dict[str, Any]:
        return {"snapshot_ids": list(self.snapshot_ids), "max_snapshots": self.max_snapshots,
                "max_pairs": self.max_pairs, "max_hash_bytes": self.max_hash_bytes}


@dataclass
class DigestMeter:
    limit: int
    consumed: int = 0
    deadline: float | None = None

    def checkpoint(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise CowRefusal("cow_periodic_deadline_exhausted")

    def debit(self, count: int) -> None:
        self.checkpoint()
        if type(count) is not int or count < 0 or self.consumed + count > self.limit:
            raise CowRefusal("cow_aggregate_digest_budget_exhausted")
        self.consumed += count


_DIGEST_METER: ContextVar[DigestMeter | None] = ContextVar("cow_digest_meter", default=None)


@contextmanager
def digest_budget(meter: DigestMeter):
    if type(meter.limit) is not int or not 1 <= meter.limit <= 128 * 1024**3:
        raise CowRefusal("cow_aggregate_digest_budget_invalid")
    token = _DIGEST_METER.set(meter)
    try:
        yield meter
    finally:
        _DIGEST_METER.reset(token)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode()).hexdigest()


def _path(path: Path, *, exists: bool = True) -> Path:
    path = path.absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise CowRefusal("symlink_component")
    if exists and not path.exists():
        raise CowRefusal("path_missing")
    return path


def _call(name: str, restype: Any, argtypes: list[Any], *args: Any) -> Any:
    if sys.platform != "darwin":
        raise CowRefusal("apfs_platform_unsupported")
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(library, name)
    except AttributeError as exc:
        raise CowRefusal("cow_metadata_or_clone_facility_unsupported") from exc
    function.restype, function.argtypes = restype, argtypes
    ctypes.set_errno(0)
    result = function(*args)
    if result == -1 or (restype == ctypes.c_void_p and not result):
        error = ctypes.get_errno()
        raise OSError(error, name + ": " + os.strerror(error))
    return result


def _metadata_scope(function):
    @wraps(function)
    def scoped(conn, *args, **kwargs):
        token = _METADATA_CONNECTION.set(conn)
        try:
            return function(conn, *args, **kwargs)
        finally:
            _METADATA_CONNECTION.reset(token)
    return scoped


def _metadata_timeout():
    budget = snapshots._reference_budget(_METADATA_CONNECTION.get())
    remaining = _METADATA_SECONDS
    if budget is not None and budget.deadline is not None:
        remaining = min(remaining, budget.deadline - time.monotonic())
    if remaining <= 0:
        if budget is not None and budget.deadline is not None:
            try:
                if budget.active:
                    budget.checkpoint()
                else:
                    # Use the original owner refusal/diagnostic and exhausted
                    # state. An initialized census deadline is never renewed.
                    with budget.census():
                        pass
            except snapshots._ReferenceBudgetExceeded as exc:
                raise CowRefusal("cow_reference_census_refused",
                                 {"cause": str(exc)[:160], "complete": False,
                                  "body_fetched": False}) from exc
        raise CowRefusal("cow_metadata_budget_exhausted")
    return remaining


def _metadata_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata member")
        value[key] = item
    return value


def _validate_metadata(value):
    integers = ("dev", "ino", "size", "allocated_bytes", "nlink", "ctime_ns",
                "mode", "uid", "gid", "mtime_ns", "flags")
    if (not isinstance(value, dict)
            or set(value) != {*integers, "birthtime", "xattrs", "acl"}
            or any(type(value[key]) is not int for key in integers)
            or value["size"] < 0 or value["allocated_bytes"] < 0
            or (value["birthtime"] is not None and
                (type(value["birthtime"]) not in (int, float)
                 or abs(value["birthtime"]) > sys.float_info.max
                 or not math.isfinite(value["birthtime"])))
            or not isinstance(value["xattrs"], dict)
            or any(not isinstance(k, str) or not isinstance(v, str)
                   or re.fullmatch(r"(?:[0-9a-f]{2})*", v) is None
                   for k, v in value["xattrs"].items())
            or (not isinstance(value["acl"], str) if sys.platform == "darwin"
                else value["acl"] is not None)):
        raise CowRefusal("cow_metadata_reply_invalid")
    return value


def _bounded_metadata(path: Path, *, directory: bool):
    encoded = os.fsencode(path.absolute())
    if len(encoded) > 8192 or b"\0" in encoded or type(directory) is not bool:
        raise CowRefusal("cow_metadata_request_invalid")
    for pid, child in list(_METADATA_UNREAPED.items()):
        if child.poll() is None:
            raise CowRefusal("cow_metadata_helper_cleanup_unknown", {"helper_pid": pid})
        _METADATA_UNREAPED.pop(pid, None)
    deadline = _metadata_clock() + _metadata_timeout()
    process = None
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        # Exact current interpreter and principal. No shell, identity override,
        # governance imports, credential environment, DB or mutation command.
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(_METADATA_HELPER), os.fsdecode(encoded),
             "1" if directory else "0"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"})
        with selectors.DefaultSelector() as selector:
            for name in streams:
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            total = 0
            while selector.get_map():
                remaining = deadline - _metadata_clock()
                if remaining <= 0:
                    raise CowRefusal("cow_metadata_timeout")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), min(65536, MAX_JSON_BYTES + 1 - total))
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > MAX_JSON_BYTES:
                        raise CowRefusal("cow_metadata_output_oversize")
                    streams[key.data].extend(data)
        try:
            code = process.wait(timeout=max(0, deadline - _metadata_clock()))
        except subprocess.TimeoutExpired as exc:
            raise CowRefusal("cow_metadata_timeout") from exc
        if streams["stderr"]:
            raise CowRefusal("cow_metadata_worker_failed")
        try:
            result = json.loads(streams["stdout"], object_pairs_hook=_metadata_object)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CowRefusal("cow_metadata_reply_invalid") from exc
        if code != 0:
            if (code != 4 or not isinstance(result, dict) or set(result) != {"error"}
                    or not isinstance(result["error"], dict)
                    or set(result["error"]) != {"type", "errno", "reason"}):
                raise CowRefusal("cow_metadata_worker_failed")
            error = result["error"]
            if not isinstance(error["type"], str) or not isinstance(error["reason"], str):
                raise CowRefusal("cow_metadata_reply_invalid")
            refusals = {"symlink_component", "path_missing", "unexpected_file_type", "hardlink_refused",
                        "apfs_platform_unsupported", "cow_metadata_or_clone_facility_unsupported",
                        "cow_xattr_facility_unsupported", "cow_xattr_list_drift", "cow_xattr_value_drift",
                        "cow_metadata_output_oversize", "cow_metadata_worker_failed"}
            if error["type"] == "refusal" and error["errno"] is None and error["reason"] in refusals:
                raise CowRefusal(error["reason"])
            if (error["type"] == "oserror" and type(error["errno"]) is int
                    and 0 < error["errno"] < 4096 and error["reason"] == "os_metadata_error"):
                raise CowRefusal("cow_metadata_os_error", {"errno": error["errno"]})
            raise CowRefusal("cow_metadata_reply_invalid")
        result = _validate_metadata(result)
        if _metadata_clock() >= deadline:
            raise CowRefusal("cow_metadata_timeout")
        _metadata_timeout()  # No success after the shared absolute budget expired.
        return result
    except OSError as exc:
        raise CowRefusal("cow_metadata_worker_failed") from exc
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired as exc:
                    # OS-uninterruptible helpers must remain accounted for;
                    # refuse further workers instead of hanging cleanup/retrying.
                    _METADATA_UNREAPED[process.pid] = process
                    raise CowRefusal("cow_metadata_helper_cleanup_unknown",
                                     {"helper_pid": process.pid}) from exc
            finally:
                process.stdout.close()
                process.stderr.close()


def _acl(path: Path) -> str:
    return _metadata(path)["acl"]


def _xattrs(path: Path) -> dict[str, str]:
    return _metadata(path)["xattrs"]


def _metadata(path: Path, *, directory: bool = False) -> dict[str, Any]:
    return _bounded_metadata(path, directory=directory)


def _same(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return actual == expected


def _hash(path: Path, expected: dict[str, Any]) -> str:
    if not _same(_metadata(path), expected):
        raise CowRefusal("hash_preimage_drift")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (expected["dev"], expected["ino"]):
            raise CowRefusal("hash_inode_drift")
        digest = hashlib.sha256()
        read_bytes = 0
        while read_bytes < expected["size"]:
            meter = _DIGEST_METER.get()
            count = min(1024 * 1024, expected["size"] - read_bytes)
            if meter is not None:
                meter.checkpoint()
                count = min(count, meter.limit - meter.consumed)
                if count <= 0:
                    raise CowRefusal("cow_aggregate_digest_budget_exhausted")
            chunk = os.read(descriptor, count)
            if not chunk:
                raise CowRefusal("hash_preimage_short_read")
            if meter is not None:
                meter.debit(len(chunk))
            digest.update(chunk)
            read_bytes += len(chunk)
    finally:
        os.close(descriptor)
    if not _same(_metadata(path), expected):
        raise CowRefusal("hash_postimage_drift")
    return digest.hexdigest()


def _fsync(path: Path, *, directory: bool = False) -> None:
    _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                         (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, value: dict[str, Any]) -> None:
    _path(path, exists=False)
    temporary = path.with_name(path.name + ".pending")
    _path(temporary, exists=False)
    with temporary.open("x") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync(path.parent, directory=True)


def _read(path: Path) -> dict[str, Any]:
    _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
            raise CowRefusal("receipt_unreadable")
        value = json.load(handle)
        after = os.fstat(handle.fileno())
    current = path.lstat()
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, k) != getattr(after, k) or getattr(before, k) != getattr(current, k) for k in keys):
        raise CowRefusal("receipt_read_drift")
    if not isinstance(value, dict):
        raise CowRefusal("receipt_malformed")
    return value


def _config(project_id: str) -> dict[str, Any]:
    value = (project_service.get_project_config_metadata(project_id).get("governance") or {}).get(
        "snapshot_cow_cleanup") or {}
    if not isinstance(value, dict):
        raise CowRefusal("cow_config_malformed")
    return value


def _budgets(config: dict[str, Any]) -> dict[str, int]:
    result = {}
    for key, default, maximum in (("max_snapshots", 1, 20), ("max_pairs", 2, 40),
                                  ("max_hash_bytes", 64 * 1024 * 1024, 4 * 1024**3)):
        value = config.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise CowRefusal("cow_budget_invalid:" + key)
        result[key] = value
    return result


def _custody(conn: sqlite3.Connection, project_id: str, root: Path) -> dict[str, Any]:
    registered = project_service.resolve_project_root(project_id, fallback_self=False)
    if registered is None or _path(registered) != _path(root):
        raise CowRefusal("cow_project_root_mismatch")
    database = [str(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main"]
    if len(database) != 1 or not database[0]:
        raise CowRefusal("cow_physical_database_required")
    from .db import _governance_root
    world = _path(_governance_root())
    expected = world / project_id / "governance.db"
    if _path(Path(database[0])) != _path(expected):
        raise CowRefusal("cow_database_world_mismatch")
    info = _metadata(expected)
    return {"project_id": project_id, "project_root": str(root.absolute()),
            "root_identity": _metadata(root, directory=True)["ino"],
            "world": str(world), "world_identity": _metadata(world, directory=True)["ino"],
            "db_path": str(expected), "db_dev": info["dev"], "db_ino": info["ino"]}


def _live_pins(conn: sqlite3.Connection, project_id: str) -> set[str]:
    budget = snapshots._reference_budget(conn)
    try:
        with budget.census() if budget else snapshots._unbudgeted_reference_census():
            with snapshots._reference_read_snapshot(conn):
                return _live_pins_in_snapshot(conn, project_id)
    except (ValueError, sqlite3.Error) as exc:
        if isinstance(exc, CowRefusal):
            raise
        if str(exc).startswith("reference_census_window_unbounded:"):
            raise CowRefusal(str(exc).replace("reference_census_window_unbounded:",
                                             "cow_live_window_unbounded:", 1)) from exc
        raise CowRefusal("cow_reference_census_refused", {"cause": str(exc)[:160],
                         "complete": False, "body_fetched": False}) from exc


def _live_pins_in_snapshot(conn: sqlite3.Connection, project_id: str) -> set[str]:
    """Dedicated typed bounded live census; completed audits do not delete bytes.

    Required schema validation is shared with cleanup, including the jointly
    optional business queue rule. Status filtering happens in SQL so historical
    ledger size does not make a supposedly complete live census a truncated one.
    Unknown status and present additional QA/lease/use stores remain protective.
    """
    from . import stale_artifact_cleanup as cleanup
    from .server import _graph_release_build_fence_state
    reasons = cleanup._archive_reference_inventory_reasons(conn, project_id=project_id)
    if reasons:
        raise CowRefusal("cow_live_inventory:" + ",".join(reasons))
    required = {"graph_snapshots": {"project_id", "snapshot_id", "snapshot_kind", "status", "created_at"},
                "graph_snapshot_refs": {"project_id", "snapshot_id", "ref_name"},
                "pending_scope_reconcile": {"project_id", "snapshot_id", "status"},
                "graph_current_full_build_claim_history": {"project_id", "snapshot_id", "status",
                    "released_at", "terminal_status", "claim_id", "run_id", "commit_sha",
                    "manager_epoch", "manager_pid", "manager_started_at", "manager_start_identity", "acquired_at"},
                "graph_query_traces": {"project_id", "snapshot_id", "status", "canonical_base_snapshot_id"},
                "reconcile_run_metrics": {"project_id", "snapshot_id", "status"},
                "graph_semantic_jobs": {"project_id", "snapshot_id", "status", "lease_expires_at"}}
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in required.items():
        if table not in names or not columns <= cleanup._table_columns(conn, table):
            raise CowRefusal("cow_live_schema_incomplete:" + table)
    if not _graph_release_build_fence_state(conn, project_id).get("clear"):
        raise CowRefusal("cow_current_full_writer_active")
    rows = conn.execute("SELECT snapshot_id FROM graph_snapshot_refs WHERE project_id=? LIMIT ?",
                        (project_id, MAX_LIVE_ROWS + 1)).fetchall()
    if len(rows) > MAX_LIVE_ROWS:
        raise CowRefusal("cow_ref_window_unbounded")
    pins = {str(r[0]) for r in rows}
    keep = snapshots.get_snapshot_retention_config(project_id)["keep_last_n"]
    retention = (project_service.get_project_config_metadata(project_id).get("governance") or {}).get("snapshot_retention") or {}
    configured_keep = retention.get("keep_last_n")
    if configured_keep is not None and (type(configured_keep) is not int or configured_keep < 1):
        raise CowRefusal("cow_retention_policy_malformed")
    if type(keep) is not int or not 1 <= keep <= MAX_LIVE_ROWS:
        raise CowRefusal("cow_retention_policy_unbounded")
    pins.update(str(r[0]) for r in conn.execute("SELECT snapshot_id FROM graph_snapshots "
        "WHERE project_id=? AND snapshot_kind IN ('scope','full') "
        "ORDER BY created_at DESC,snapshot_id DESC LIMIT ?", (project_id, keep)))
    newest = conn.execute("SELECT snapshot_id FROM graph_snapshots WHERE project_id=? "
        "AND snapshot_kind='full' ORDER BY created_at DESC,snapshot_id DESC LIMIT 1", (project_id,)).fetchone()
    if newest:
        pins.add(str(newest[0]))
    config = _config(project_id)
    # Only named, bounded manifests. Never walk/hash the snapshot history.
    from .self_graph_bundle_check import SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH
    manifests = [Path(__file__).resolve().parents[2] / SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH]
    configured = config.get("bundle_manifests", [])
    if not isinstance(configured, list) or len(configured) > 16:
        raise CowRefusal("cow_bundle_manifest_inventory_unbounded")
    manifests.extend(Path(p) for p in configured)
    for path in manifests:
        if not path.exists() and path == manifests[0]:
            continue  # Source packages may have no sealed bundle; configured missing paths refuse.
        manifest = _read(path)
        sid = manifest.get("snapshot_id")
        if not isinstance(sid, str):
            raise CowRefusal("cow_bundle_manifest_malformed")
        pins.add(sid)
    history = {"task_timeline_events", "release_operator_head_queue_events"}
    stores = (set(cleanup._ARCHIVE_REFERENCE_COLUMNS) | set(required)) - history - {
        "graph_snapshots", "graph_snapshot_refs"}
    stores.update(n for n in names if any(part in n for part in ("qa_session", "semantic_use", "lease")))
    known_ids = {str(r[0]) for r in conn.execute(
        "SELECT snapshot_id FROM graph_snapshots WHERE project_id=?", (project_id,))}
    reference_tokens = snapshots._snapshot_reference_tokens(conn, project_id, known_ids)
    # The native token builder has already bounded/validated this ID inventory.
    legacy_tokens = [(sid, str(snapshots._snapshot_root(project_id, sid))) for sid in known_ids]
    if "graph_semantic_projections" in names:
        if not {"project_id", "snapshot_id", "status", "projection_json"} <= cleanup._table_columns(conn, "graph_semantic_projections"):
            raise CowRefusal("cow_semantic_projection_schema_incomplete")
        stores.add("graph_semantic_projections")
    if project_id == "aming-claw":
        stores -= cleanup._STABLE_QUEUE_REFERENCE_TABLES
    terminals = tuple(sorted(TERMINAL))
    scanned = 0
    for table in sorted(stores & names):
        columns = cleanup._table_columns(conn, table)
        if table == "contract_runtime_executions":
            for reference in snapshots._contract_reference_rows(
                    conn, project_id, known_ids, current_only=True):
                scanned += 1
                state, metadata = reference["state"], reference["metadata"]
                if state not in {"completed", "live"}:
                    cause = "null" if metadata["sqlite_storage_type"] == "null" else state
                    reason = ("cow_live_payload_unbounded:" + table if cause == "nontext" else
                              "cow_live_payload_malformed:" + table if cause == "malformed_json" else
                              "cow_contract_live_state_unknown")
                    raise CowRefusal(reason, {**metadata, "cause": cause})
                if state == "completed":
                    continue  # Current-use skip only; ordinary durable audit remains protected.
                if not reference["complete"]:
                    raise CowRefusal("cow_live_payload_unbounded:" + table,
                        {**metadata, "cause": "typed_projection_incomplete", "complete": False})
                pins.update(reference["pins"])
                if scanned > 20000 or len(pins) > snapshots._REFERENCE_MAX_ROWS or sum(
                        len(pin.encode('utf-8')) for pin in pins) > MAX_JSON_BYTES:
                    raise CowRefusal("cow_live_census_unbounded")
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        unknown_owner = table not in (set(cleanup._ARCHIVE_REFERENCE_COLUMNS) | set(required) |
                                      {"graph_semantic_projections"})
        if unknown_owner:
            # No payload transfer from an owner whose field/disposition contract
            # is unknown. An empty store is proved empty in this read snapshot.
            if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                if not {"snapshot_id", "status"} <= columns:
                    raise CowRefusal("cow_live_owner_schema_unknown:" + table,
                        {"table": table, "cause": "owner_schema_unknown", "complete": False,
                         "body_fetched": False})
                pins.update(known_ids)  # Unverified terminal labels never remove a use pin.
            continue
        where, args = [], []
        if "project_id" in columns:
            where.append("project_id=?")
            args.append(project_id)
        if "status" in columns:
            statuses = terminals
            if table == "graph_semantic_jobs":
                from .reconcile_semantic_enrichment import SEMANTIC_JOB_TERMINAL_STATUSES
                statuses = tuple(sorted(TERMINAL | SEMANTIC_JOB_TERMINAL_STATUSES))
            predicate = "lower(coalesce(status,'')) NOT IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
            if table == "graph_current_full_build_claim_history":
                # Recognize the atomic native release only with complete owner
                # identity and UTC proof. No historical manager-liveness check.
                # Filter in SQL before LIMIT; NULL/malformed proof stays live.
                released = ["status COLLATE BINARY='released'",
                            "terminal_status COLLATE BINARY IN ('candidate_ready','failed')",
                            "typeof(manager_pid)='integer' AND manager_pid>0",
                            "length(commit_sha) IN (40,64) AND commit_sha NOT GLOB '*[^0-9a-f]*'",
                            "length(manager_start_identity)=71 "
                            "AND substr(manager_start_identity,1,7) COLLATE BINARY='sha256:' "
                            "AND substr(manager_start_identity,8) NOT GLOB '*[^0-9a-f]*'"]
                # TEXT affinity permits BLOB; string functions can truncate at
                # NUL. Require actual TEXT without NUL for every textual proof.
                # Match Python str.strip(), including Unicode whitespace and
                # U+001C..U+001F separators, while preserving interior Unicode.
                strip_chars = ("char(9,10,11,12,13,28,29,30,31,32,133,160,5760,"
                               "8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,"
                               "8232,8233,8239,8287,12288)")
                for field in ("claim_id", "project_id", "snapshot_id", "run_id", "manager_epoch",
                              "commit_sha", "manager_start_identity", "released_at", "manager_started_at",
                              "acquired_at", "status", "terminal_status"):
                    released.append(f"typeof({field})='text' AND length({field})>0 "
                        f"AND {field}=trim({field},{strip_chars}) COLLATE BINARY "
                        f"AND instr({field},char(0))=0")
                for field in ("released_at", "manager_started_at", "acquired_at"):
                    # Julian round-trip rejects invalid calendar days and 24:00
                    # normalization, which direct strftime can silently accept.
                    released.append(f"length({field})=20 AND substr({field},1,4)>='0001' "
                        f"AND strftime('%Y-%m-%dT%H:%M:%SZ',julianday({field}))={field} COLLATE BINARY")
                predicate = "(" + predicate + " AND NOT coalesce(" + " AND ".join(released) + ",0))"
            if "lease_expires_at" in columns:
                predicate = "(" + predicate + " OR coalesce(lease_expires_at,'')>?)"
                args.append(datetime.now(timezone.utc).isoformat())
            where.append(predicate)
        # Project byte/type facts before transferring JSON bodies to Python.
        # SQLite TEXT length counts characters (and stops at NUL); its BLOB
        # representation measures actual UTF-8 bytes. BLOB is never called TEXT.
        fields = sorted(columns)
        json_fields = [key for key in fields if key.endswith("_json")]
        projection = []
        for key in fields:
            column = '"' + key.replace('"', '""') + '"'
            projection.append(
                f"CASE WHEN typeof({column})='text' "
                f"AND length(CAST({column} AS BLOB))<={MAX_JSON_BYTES} "
                f"THEN {column} ELSE NULL END AS {column}"
                if key in json_fields else column)
        for key in json_fields:
            column = '"' + key.replace('"', '""') + '"'
            projection.extend((f"typeof({column})", f"length(CAST({column} AS BLOB))"))
        records = snapshots._reference_pages(conn, table, ','.join(projection),
                                             " AND ".join(where), tuple(args), max_rows=MAX_LIVE_ROWS)
        for source_record in records:
            record = source_record[1:]
            scanned += 1
            if scanned > 20000:
                raise CowRefusal("cow_live_census_unbounded")
            value = dict(zip(fields, record[:len(fields)]))
            payload_metadata = {}
            for index, key in enumerate(json_fields):
                storage_type, byte_length = record[len(fields) + index * 2:len(fields) + index * 2 + 2]
                payload_metadata[key] = {
                    "table": table, "field": key, "sqlite_storage_type": storage_type,
                    "storage_byte_length": byte_length,
                    "utf8_byte_length": byte_length if storage_type == "text" else None,
                    "execution_identity_hash": ("sha256:" + _digest(
                        [table, str(value["contract_execution_id"])]))
                        if table == "contract_runtime_executions" else None,
                    "body_fetched": storage_type == "text" and byte_length <= MAX_JSON_BYTES,
                }
            lease = value.get("lease_expires_at")
            if lease:
                try:
                    parsed_lease = datetime.fromisoformat(str(lease).replace("Z", "+00:00"))
                    if parsed_lease.tzinfo is None:
                        raise ValueError("timezone missing")
                except (ValueError, TypeError):
                    raise CowRefusal("cow_lease_malformed:" + table)
            if table == "session_context":
                task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (value["task_id"],)).fetchone()
                if not task:
                    raise CowRefusal("cow_session_task_unknown")
                if str(task[0]).lower() in TERMINAL:
                    continue
            for key, raw in value.items():
                if key in payload_metadata:
                    metadata = payload_metadata[key]
                    if metadata["sqlite_storage_type"] == "null" or (
                            metadata["sqlite_storage_type"] == "text" and metadata["storage_byte_length"] == 0):
                        continue  # Existing optional empty/null payload semantics.
                    if metadata["sqlite_storage_type"] != "text" or metadata["storage_byte_length"] > MAX_JSON_BYTES:
                        cause = "nontext" if metadata["sqlite_storage_type"] != "text" else "oversized_text"
                        raise CowRefusal("cow_live_payload_unbounded:" + table, {**metadata, "cause": cause})
                    try:
                        payload = json.loads(raw)  # Corruption is never an empty inventory.
                    except (ValueError, TypeError, UnicodeError) as exc:
                        raise CowRefusal("cow_live_payload_malformed:" + table,
                                         {**metadata, "cause": "malformed_json"}) from exc
                    try:
                        pins.update(snapshots._bounded_decoded_reference_pins(payload, reference_tokens))
                    except (ValueError, RecursionError) as exc:
                        raise CowRefusal("cow_live_payload_unbounded:" + table,
                            {**metadata, "cause": "typed_projection_incomplete", "complete": False}) from exc
            # Preserve complete-row ID/path substring matching, retaining only
            # matched identities rather than every unrelated serialized body.
            serialized = json.dumps(value, sort_keys=True, default=str)
            if len(serialized.encode('utf-8')) > MAX_JSON_BYTES:
                raise CowRefusal("cow_live_census_unbounded")
            for sid, root in legacy_tokens:
                snapshots._reference_checkpoint(conn)
                if sid in serialized or root in serialized:
                    pins.add(sid)
            if len(pins) > snapshots._REFERENCE_MAX_ROWS or sum(
                    len(pin.encode('utf-8')) for pin in pins) > MAX_JSON_BYTES:
                raise CowRefusal("cow_live_census_unbounded")
    return pins


def _eligible(conn: sqlite3.Connection, project_id: str, sid: str,
              pins: set[str]) -> None:
    if not snapshots._snapshot_id_is_component(sid):
        raise CowRefusal("cow_snapshot_id_invalid")
    rows = conn.execute("SELECT snapshot_kind,status FROM graph_snapshots "
                        "WHERE project_id=? AND snapshot_id=?", (project_id, sid)).fetchall()
    if [tuple(r) for r in rows] != [("full", "superseded")]:
        raise CowRefusal("cow_snapshot_not_owned_superseded_full")
    root = str(snapshots._snapshot_root(project_id, sid))
    if any(sid in pin or root in pin for pin in pins):
        raise CowRefusal("cow_snapshot_live_or_retained")
    if project_id == "aming-claw":
        from . import stale_artifact_cleanup as cleanup
        _, reasons = cleanup._stable_release_queue_reference_facts(tokens=(sid, root))
        reasons = [r for r in reasons if r != "referenced_by_stable_release_operator_head_queue_events"]
        if reasons:
            raise CowRefusal("cow_stable_queue:" + ",".join(reasons))


def _pair(project_id: str, sid: str, relative: tuple[str, str]) -> dict[str, Any]:
    root = _path(snapshots._snapshot_root(project_id, sid))
    source, target = root / relative[0], root / relative[1]
    sm, tm = _metadata(source), _metadata(target)
    if sm["dev"] != tm["dev"] or sm["ino"] == tm["ino"] or sm["size"] != tm["size"]:
        raise CowRefusal("cow_pair_identity_or_size_mismatch")
    source_hash, target_hash = _hash(source, sm), _hash(target, tm)
    if source_hash != target_hash:
        raise CowRefusal("cow_pair_content_differs")
    return {"candidate_id": "cow-" + _digest([project_id, sid, relative])[:24],
            "snapshot_id": sid, "source": relative[0], "target": relative[1],
            "sha256": source_hash, "source_metadata": sm, "target_metadata": tm,
            "directories": {str(d.relative_to(root)): _metadata(d, directory=True)
                            for d in (source.parent, target.parent)},
            "snapshot_identity": _metadata(root, directory=True), "safe_to_apply": True}


@_metadata_scope
def preview(conn: sqlite3.Connection, project_id: str, root: Path, *,
            _run_selection: RunSelection | None = None) -> dict[str, Any]:
    custody = _custody(conn, project_id, root)
    config = _config(project_id)
    budgets = _budgets(config)
    if _run_selection is not None:
        if not isinstance(_run_selection, RunSelection):
            raise CowRefusal("cow_run_selection_invalid")
        budgets = _run_selection.budgets(budgets)
    pins = _live_pins(conn, project_id)
    explicit = list(_run_selection.snapshot_ids) if _run_selection else config.get("snapshot_ids")
    where, args = "", []
    if explicit is not None:
        if (not isinstance(explicit, list) or not explicit or len(explicit) > budgets["max_snapshots"]
                or len(set(explicit)) != len(explicit)
                or any(not isinstance(sid, str) or not snapshots._snapshot_id_is_component(sid) for sid in explicit)):
            raise CowRefusal("cow_bounded_snapshot_selection_invalid")
        where = " AND snapshot_id IN (" + ",".join("?" for _ in explicit) + ")"
        args = explicit
    selected = conn.execute("SELECT snapshot_id FROM graph_snapshots WHERE project_id=? "
        "AND snapshot_kind='full' AND status='superseded'" + where +
        " ORDER BY created_at,snapshot_id LIMIT ?",
        (project_id, *args, budgets["max_snapshots"] + 1)).fetchall()
    if explicit is not None and {r[0] for r in selected} != set(explicit):
        raise CowRefusal("cow_selected_snapshot_missing_or_not_superseded_full")
    incomplete = len(selected) > budgets["max_snapshots"]
    rows, rejected, hashed = [], [], 0
    for selected_row in selected[:budgets["max_snapshots"]]:
        sid = str(selected_row[0])
        try:
            _eligible(conn, project_id, sid, pins)
        except CowRefusal as exc:
            rejected.append({"snapshot_id": sid, "reason": str(exc)})
            continue
        for relative in PAIRS:
            if len(rows) >= budgets["max_pairs"]:
                incomplete = True
                break
            try:
                base = snapshots._snapshot_root(project_id, sid)
                sizes = [_metadata(base / rel)["size"] for rel in relative]
                if hashed + sum(sizes) > budgets["max_hash_bytes"]:
                    incomplete = True
                    break
                hashed += sum(sizes)
                pair = _pair(project_id, sid, relative)
                completed = _state_root(project_id) / (pair["candidate_id"] + ".complete.json")
                if completed.exists():
                    receipt = _read(completed)
                    if receipt.get("custody") != custody or receipt.get("entry", {}).get("candidate", {}).get("candidate_id") != pair["candidate_id"]:
                        raise CowRefusal("cow_completion_index_scope_drift")
                    entry = receipt["entry"]
                    original = entry["candidate"]
                    directory_keys = ("dev", "ino", "birthtime", *PRESERVED)
                    # _pair already hashed both files against stable identities.
                    # Reuse those proofs instead of exceeding the hash budget.
                    matching = (entry.get("phase") == "complete"
                        and pair["sha256"] == original["sha256"]
                        and pair["source_metadata"] == original["source_metadata"]
                        and pair["target_metadata"] == entry.get("replacement")
                        and pair["snapshot_identity"] == original["snapshot_identity"]
                        and all(pair["directories"][rel][key] == expected[key]
                                for rel, expected in original["directories"].items()
                                for key in directory_keys))
                    if matching:
                        rejected.append({"snapshot_id": sid, "reason": "matching_native_completion"})
                        continue
                rows.append(pair)
            except (CowRefusal, OSError) as exc:
                rejected.append({"snapshot_id": sid, "reason": str(exc).split(":")[0]})
    if _custody(conn, project_id, root) != custody:
        raise CowRefusal("cow_custody_drift")
    plan = {"custody": custody, "budgets": budgets, "config_hash": _digest(config),
            "candidates": rows, "incomplete": incomplete}
    if _run_selection is not None:
        plan["run_selection"] = _run_selection.receipt()
    plan_hash = "sha256:" + _digest(plan)
    operation_id = "cowop-" + _digest([custody, plan_hash])[:32]
    for row in rows:
        row["safe_to_apply"] = not incomplete
    unique = {(p["dev"], p["ino"]): p["allocated_bytes"] for row in rows
              for p in (row["source_metadata"], row["target_metadata"])}
    return {"ok": True, "mode": "dry_run", "dimension": DIMENSION,
            "project_id": project_id, "plan_revision": REVISION, "plan_hash": plan_hash,
            "reference_census": {"complete": True, "read_snapshot": "same_connection",
                                 "page_rows": snapshots._REFERENCE_PAGE_ROWS,
                                 "max_rows_per_store": MAX_LIVE_ROWS},
            "operation_id": operation_id, "custody": custody, "budgets": budgets,
            "config_hash": _digest(config), "candidates": rows, "refusals": rejected,
            "apply_plan_available": not incomplete, "writes_performed": False,
            "write_disposition": "not_written", "summary": {
                "candidate_count": len(rows), "safe_apply_count": len(rows) if not incomplete else 0,
                "truncated": incomplete, "hash_bytes": hashed,
                "apparent_bytes": sum(p["size"] for row in rows for p in
                                      (row["source_metadata"], row["target_metadata"])),
                "unique_inode_allocated_bytes": sum(unique.values()),
                "duplicate_byte_potential": sum(r["target_metadata"]["allocated_bytes"] for r in rows),
                "physical_reclaim": "UNKNOWN"}}


def _volume(path: Path) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise CowRefusal("apfs_platform_unsupported")
    try:
        path = _path(path)
        before = path.lstat()
        # diskutil accepts a mounted device, but not an arbitrary directory.
        # df resolves APFS firmlinks too; walking lexical parents does not.
        inventory = subprocess.run(["df", "-P", str(path)], capture_output=True,
                                   check=True, timeout=10)
        lines = inventory.stdout.decode("utf-8").splitlines()
        node = lines[1].split()[0] if len(lines) == 2 and lines[1].split() else ""
        if not re.fullmatch(r"/dev/disk[0-9]+(?:s[0-9]+)*", node):
            raise CowRefusal("cow_volume_device_unverified")
        device = _path(Path(node)).lstat()
        if not stat.S_ISBLK(device.st_mode) or device.st_rdev != before.st_dev:
            raise CowRefusal("cow_volume_device_mismatch")
        result = subprocess.run(["diskutil", "info", "-plist", node],
                                capture_output=True, check=True, timeout=10)
        info = plistlib.loads(result.stdout)
        if not isinstance(info, dict) or info.get("Error"):
            raise CowRefusal("cow_volume_unreadable")
        if str(info.get("FilesystemType") or "").lower() != "apfs":
            raise CowRefusal("apfs_filesystem_required")
        mount = Path(str(info.get("MountPoint") or ""))
        if (info.get("DeviceNode") != node or not info.get("VolumeUUID")
                or not mount.is_absolute()):
            raise CowRefusal("cow_volume_identity_unverified")
        mounted = _path(mount).lstat()
        after = _path(path).lstat()
        device_after = _path(Path(node)).lstat()
        if (not stat.S_ISDIR(mounted.st_mode) or mounted.st_dev != before.st_dev
                or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                or (device_after.st_dev, device_after.st_ino, device_after.st_rdev)
                != (device.st_dev, device.st_ino, device.st_rdev)):
            raise CowRefusal("cow_volume_identity_drift")
        return {"uuid": info["VolumeUUID"], "mount": str(mount), "device": before.st_dev}
    except (OSError, subprocess.SubprocessError, ValueError, ExpatError) as exc:
        if isinstance(exc, CowRefusal):
            raise
        raise CowRefusal("cow_volume_unreadable") from exc


def _archive(project_id: str, source: Path, bytes_needed: int = 0) -> dict[str, Any]:
    config = _config(project_id)
    path = Path(str(config.get("archive_root") or ""))
    mount = Path(str(config.get("archive_mount") or ""))
    if not path.is_absolute() or not mount.is_absolute() or mount == Path(mount.anchor):
        raise CowRefusal("cow_external_archive_not_configured")
    try:
        _path(path)
        _path(mount)
    except (CowRefusal, OSError) as exc:
        raise CowRefusal("cow_archive_unavailable") from exc
    if not path.is_relative_to(mount) or not os.path.ismount(mount):
        raise CowRefusal("cow_archive_mount_unverified")
    volume = _volume(path)
    if (not config.get("archive_volume_uuid") or volume["uuid"] != config["archive_volume_uuid"]
            or volume["mount"] != str(mount) or path.stat().st_dev == source.stat().st_dev):
        raise CowRefusal("cow_archive_volume_mismatch")
    if shutil.disk_usage(path).free < bytes_needed:
        raise CowRefusal("cow_archive_capacity_insufficient")
    return {"path": str(path), "identity": _metadata(path, directory=True)["ino"],
            "mount_identity": _metadata(mount, directory=True)["ino"], **volume}


def _preserve(original: Path, destination: Path, wanted: dict[str, Any]) -> None:
    _path(original)
    _path(destination)
    _call("copyfile", ctypes.c_int,
          [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32],
          os.fsencode(original), os.fsencode(destination), None, 1)  # ACL only, never bytes.
    for name in _xattrs(destination):
        if name not in wanted["xattrs"]:
            _call("removexattr", ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int],
                  os.fsencode(destination), os.fsencode(name), 0x41)
    for name, value in wanted["xattrs"].items():
        raw = bytes.fromhex(value)
        buffer = ctypes.create_string_buffer(raw)
        _call("setxattr", ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
              ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int], os.fsencode(destination),
              os.fsencode(name), buffer, len(raw), 0, 0x41)
    os.chmod(destination, wanted["mode"], follow_symlinks=False)
    info = destination.stat()
    if (info.st_uid, info.st_gid) != (wanted["uid"], wanted["gid"]):
        os.chown(destination, wanted["uid"], wanted["gid"], follow_symlinks=False)
    if info.st_flags != wanted["flags"]:
        os.chflags(destination, wanted["flags"], follow_symlinks=False)
    os.utime(destination, ns=(info.st_atime_ns, wanted["mtime_ns"]), follow_symlinks=False)
    if any(_metadata(destination)[key] != wanted[key] for key in PRESERVED):
        raise CowRefusal("cow_metadata_copy_failed")


def _copy(source: Path, destination: Path, wanted: dict[str, Any], digest: str) -> dict[str, Any]:
    _path(destination, exists=False)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        source_fd = os.open(_path(source), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    count = os.write(descriptor, view)
                    if count <= 0:
                        raise CowRefusal("cow_backup_short_write")
                    view = view[count:]
        finally:
            os.close(source_fd)
    finally:
        os.close(descriptor)
    _preserve(source, destination, wanted)
    _fsync(destination)
    info = _metadata(destination)
    if _hash(destination, info) != digest:
        raise CowRefusal("cow_backup_hash_mismatch")
    return info


def _directories(root: Path, expected: dict[str, Any], *, restore: bool,
                 allow_mtime_drift: bool = False) -> None:
    for relative, wanted in expected.items():
        path = root / relative
        actual = _metadata(path, directory=True)
        keys = ("dev", "ino", "birthtime", "mode", "uid", "gid", "flags", "xattrs", "acl")
        if any(actual[key] != wanted[key] for key in keys):
            raise CowRefusal("cow_directory_identity_drift")
        if restore:
            os.utime(path, ns=(path.stat().st_atime_ns, wanted["mtime_ns"]), follow_symlinks=False)
            _fsync(path, directory=True)
        if not allow_mtime_drift and _metadata(path, directory=True)["mtime_ns"] != wanted["mtime_ns"]:
            raise CowRefusal("cow_directory_mtime_drift")


def _quiet(source: Path, target: Path) -> None:
    result = subprocess.run(["lsof", "-nP", "--", str(source), str(target)],
                            capture_output=True, text=True, check=False, timeout=10)
    if result.returncode not in (0, 1) or result.stdout.strip() or result.stderr.strip():
        raise CowRefusal("cow_pair_open_writer_or_reader")


def _clone(source: Path, stage: Path) -> None:
    _path(source)
    _path(stage, exists=False)
    if stage.exists():
        raise CowRefusal("cow_stage_exists")
    if source.stat().st_dev != stage.parent.stat().st_dev:
        raise CowRefusal("cow_cross_device")
    _call("clonefile", ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32],
          os.fsencode(source), os.fsencode(stage), CLONE_NOFOLLOW_ANY)


def _state_root(project_id: str, *, create: bool = False) -> Path:
    root = snapshots._snapshot_root(project_id, "_sentinel").parent.parent / "snapshot-cow-cleanup"
    _path(root, exists=False)
    if create and not root.exists():
        root.mkdir(mode=0o700)
        _fsync(root.parent, directory=True)
    return root


def _journal_path(project_id: str, operation_id: str) -> Path:
    if not isinstance(operation_id, str) or not re.fullmatch(r"cowop-[0-9a-f]{32}", operation_id):
        raise CowRefusal("cow_operation_id_invalid")
    return _state_root(project_id) / (operation_id + ".json")


def _operation(path: Path, project_id: str) -> dict[str, Any]:
    record = _read(path)
    entries = record.get("entries")
    if (record.get("schema") != "snapshot_cow_operation.v1" or not isinstance(entries, list)
            or not 1 <= len(entries) <= 40 or record.get("plan_revision") != REVISION
            or not isinstance(record.get("custody"), dict)
            or not isinstance(record.get("candidate_ids"), list)):
        raise CowRefusal("cow_operation_record_malformed")
    ids = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("candidate"), dict):
            raise CowRefusal("cow_operation_entry_malformed")
        row = entry["candidate"]
        relative = (row.get("source"), row.get("target"))
        sid = row.get("snapshot_id")
        if (not isinstance(sid, str) or not snapshots._snapshot_id_is_component(sid)
                or relative not in PAIRS
                or row.get("candidate_id") != "cow-" + _digest([project_id, sid, relative])[:24]
                or not isinstance(row.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or set(row.get("directories", {})) != {str(Path(p).parent) for p in relative}
                or not isinstance(row.get("source_metadata"), dict)
                or not isinstance(row.get("target_metadata"), dict)
                or not isinstance(row.get("snapshot_identity"), dict)):
            raise CowRefusal("cow_operation_pair_scope_malformed")
        if entry.get("stage_name") and not re.fullmatch(r"\.snapshot-cow-[0-9a-f]{32}", entry["stage_name"]):
            raise CowRefusal("cow_operation_stage_scope_malformed")
        ids.append(row["candidate_id"])
    if ids != record["candidate_ids"] or len(set(ids)) != len(ids):
        raise CowRefusal("cow_operation_candidate_inventory_malformed")
    return record


def _backup(project_id: str, operation_id: str, row: dict[str, Any]) -> dict[str, Any]:
    root = snapshots._snapshot_root(project_id, row["snapshot_id"])
    target = root / row["target"]
    wanted = row["target_metadata"]
    volume = _archive(project_id, target, wanted["size"] * 2 + 1024 * 1024)
    archive = Path(volume["path"]) / ("snapshot-cow-" + _digest(project_id)[:16])
    _path(archive, exists=False)
    archive.mkdir(exist_ok=True, mode=0o700)
    archive = archive / operation_id
    _path(archive, exists=False)
    archive.mkdir(exist_ok=True, mode=0o700)
    path = archive / (row["candidate_id"] + ".backup")
    if path.exists() or path.with_suffix(".receipt.json").exists():
        raise CowRefusal("cow_unjournaled_backup_exists")
    before = _metadata(target)
    if before != wanted or _hash(target, before) != row["sha256"]:
        raise CowRefusal("cow_backup_source_drift")
    backed = _copy(target, path, wanted, row["sha256"])
    restore_path = archive / (row["candidate_id"] + ".restore-proof")
    restored = _copy(path, restore_path, wanted, row["sha256"])
    if (any(restored[k] != wanted[k] or backed[k] != wanted[k] for k in PRESERVED)
            or _metadata(target) != before or _hash(target, before) != row["sha256"]
            or _archive(project_id, target) != volume):
        raise CowRefusal("cow_external_restore_proof_failed")
    receipt = {"schema": "snapshot_cow_backup.v1", "project_id": project_id,
               "operation_id": operation_id, "candidate_id": row["candidate_id"],
               "volume": volume, "path": str(path), "backup_metadata": backed,
               "sha256": row["sha256"], "target_metadata": wanted,
               "directory_metadata": row["directories"], "restore_proof": restored,
               "restore_verified": True}
    _write(path.with_suffix(".receipt.json"), receipt)
    _fsync(archive.parent, directory=True)
    restore_path.unlink()
    _fsync(archive, directory=True)
    return receipt


def _verify_backup(project_id: str, row: dict[str, Any], receipt: dict[str, Any]) -> Path:
    root = snapshots._snapshot_root(project_id, row["snapshot_id"])
    if (_archive(project_id, root) != receipt["volume"] or not receipt.get("restore_verified")
            or receipt.get("project_id") != project_id
            or receipt.get("candidate_id") != row["candidate_id"]
            or receipt.get("sha256") != row["sha256"]
            or receipt.get("target_metadata") != row["target_metadata"]
            or receipt.get("directory_metadata") != row["directories"]):
        raise CowRefusal("cow_backup_receipt_scope_drift")
    path = Path(receipt["path"])
    expected_parent = Path(receipt["volume"]["path"]) / ("snapshot-cow-" + _digest(project_id)[:16]) / receipt["operation_id"]
    if path != expected_parent / (row["candidate_id"] + ".backup"):
        raise CowRefusal("cow_backup_path_scope_drift")
    if _read(path.with_suffix(".receipt.json")) != receipt:
        raise CowRefusal("cow_backup_receipt_drift")
    if _metadata(path) != receipt["backup_metadata"] or _hash(path, receipt["backup_metadata"]) != row["sha256"]:
        raise CowRefusal("cow_backup_preimage_drift")
    return path


def _fresh(conn: sqlite3.Connection, project_id: str, root: Path,
           record: dict[str, Any], row: dict[str, Any], *, recovery: bool = False) -> None:
    meter = _DIGEST_METER.get()
    if meter is not None:
        meter.checkpoint()
    if (_custody(conn, project_id, root) != record["custody"]
            or _digest(_config(project_id)) != record["config_hash"]):
        raise CowRefusal("cow_custody_or_config_drift")
    _eligible(conn, project_id, row["snapshot_id"], _live_pins(conn, project_id))
    base = _path(snapshots._snapshot_root(project_id, row["snapshot_id"]))
    _directories(base, row["directories"], restore=False, allow_mtime_drift=recovery)
    # Snapshot directory itself is never replaced or touched by file staging.
    if _metadata(base, directory=True) != row["snapshot_identity"]:
        raise CowRefusal("cow_snapshot_directory_drift")
    source, target = base / row["source"], base / row["target"]
    _quiet(source, target)  # The server's DB connection is allowed under native locks.
    if _hash(source, row["source_metadata"]) != row["sha256"]:
        raise CowRefusal("cow_source_drift")


def _completed_readback(project_id: str, entry: dict[str, Any]) -> None:
    row = entry["candidate"]
    base = snapshots._snapshot_root(project_id, row["snapshot_id"])
    _directories(base, row["directories"], restore=False)
    if (_hash(base / row["source"], row["source_metadata"]) != row["sha256"]
            or _hash(base / row["target"], entry["replacement"]) != row["sha256"]):
        raise CowRefusal("cow_completed_fingerprint_drift")
    if (entry["replacement"]["ino"] in (row["source_metadata"]["ino"], row["target_metadata"]["ino"])
            or entry["replacement"]["nlink"] != 1
            or any(entry["replacement"][k] != row["target_metadata"][k] for k in PRESERVED)):
        raise CowRefusal("cow_completed_metadata_drift")


def _result(record: dict[str, Any], *, replay: bool = False) -> dict[str, Any]:
    entries = record["entries"]
    completed = [e["candidate"]["candidate_id"] for e in entries if e["phase"] == "complete"]
    ambiguous = any(e["phase"] in {"before_replace", "after_replace"} for e in entries)
    graph_written = bool(completed) or any(e.get("replacement") for e in entries)
    return {"ok": record["state"] == "complete", "dimension": DIMENSION,
            "project_id": record["custody"]["project_id"], "operation_id": record["operation_id"],
            "state": record["state"], "replay": replay, "applied_count": len(completed),
            "applied_candidate_ids": completed, "writes_performed": False if replay else True,
            "graph_files_replaced": len([e for e in entries if e.get("replacement")]),
            "write_disposition": "not_written" if replay else "ambiguous" if ambiguous else
            "written", "safe_retry": False,
            "journal_persisted": True, "backup_retained": True,
            "error": record.get("error"), "filesystem": record.get("filesystem"),
            "pair_observations": [{"candidate_id": e["candidate"]["candidate_id"],
                                   "phase": e["phase"], "filesystem": e.get("filesystem"),
                                   "retained_stage_name": e.get("stage_name"),
                                   "new_inode": (e.get("replacement") or {}).get("ino"),
                                   "new_birthtime": (e.get("replacement") or {}).get("birthtime"),
                                   "new_ctime_ns": (e.get("replacement") or {}).get("ctime_ns")}
                                  for e in entries],
            "measurement_note": "Available-byte deltas include background activity; st_blocks and df do not identify shared extents or exclusive reclaimed blocks."}


@_metadata_scope
def apply(conn: sqlite3.Connection, project_id: str, root: Path, *, candidate_ids: list[str],
          plan_hash: str, plan_revision: int, operation_id: str,
          _run_selection: RunSelection | None = None) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise CowRefusal("apfs_platform_unsupported")
    if (type(plan_revision) is not int or plan_revision != REVISION or not candidate_ids
            or len(candidate_ids) > 40 or len(set(candidate_ids)) != len(candidate_ids)):
        raise CowRefusal("cow_exact_plan_required")
    custody = _custody(conn, project_id, root)
    path = _journal_path(project_id, operation_id)
    if path.with_name(path.name + ".pending").exists():
        raise CowRefusal("cow_journal_pending_inspect_required")
    if path.exists():
        record = _operation(path, project_id)
        if (record.get("schema") != "snapshot_cow_operation.v1" or record.get("custody") != custody
                or record.get("operation_id") != operation_id or record.get("plan_hash") != plan_hash
                or record.get("candidate_ids") != candidate_ids
                or record.get("config_hash") != _digest(_config(project_id))
                or record.get("run_selection") != (_run_selection.receipt() if _run_selection else None)):
            raise CowRefusal("cow_operation_scope_or_replay_drift")
        if record["state"] != "complete":
            return {**_result(record), "ok": False, "error": "cow_operation_inspect_required",
                    "writes_performed": False, "write_disposition": "not_written"}
        for entry in record["entries"]:
            _completed_readback(project_id, entry)
        return _result(record, replay=True)
    projection = preview(conn, project_id, root, _run_selection=_run_selection)
    if (projection["operation_id"] != operation_id or projection["plan_hash"] != plan_hash
            or not projection["apply_plan_available"]):
        raise CowRefusal("cow_plan_stale_or_incomplete")
    candidates = {r["candidate_id"]: r for r in projection["candidates"]}
    if any(cid not in candidates or not candidates[cid]["safe_to_apply"] for cid in candidate_ids):
        raise CowRefusal("cow_candidate_not_in_exact_plan")
    rows = [candidates[cid] for cid in candidate_ids]
    try:
        _volume(snapshots._snapshot_root(project_id, rows[0]["snapshot_id"]))
        # Verify all prerequisites before even creating the local operation ledger.
        for row in rows:
            _fresh(conn, project_id, root, projection, row)
            _archive(project_id, snapshots._snapshot_root(project_id, row["snapshot_id"]),
                     row["target_metadata"]["size"] * 2 + 1024 * 1024)
    except CowRefusal as exc:
        raise CowPrejournalRefusal(str(exc), exc.metadata) from exc
    _state_root(project_id, create=True)
    before = shutil.disk_usage(snapshots._snapshot_root(project_id, rows[0]["snapshot_id"])).free
    record = {"schema": "snapshot_cow_operation.v1", "operation_id": operation_id,
              "custody": custody, "config_hash": projection["config_hash"],
              "plan_hash": plan_hash, "plan_revision": REVISION, "candidate_ids": candidate_ids,
              "entries": [{"candidate": row, "phase": "planned"} for row in rows],
              "state": "running", "filesystem": {"available_before": before}}
    if _run_selection is not None:
        record["run_selection"] = _run_selection.receipt()
    try:
        _write(path, record)
        for entry in record["entries"]:
            row = entry["candidate"]
            _fresh(conn, project_id, root, record, row)
            base = snapshots._snapshot_root(project_id, row["snapshot_id"])
            source, target = base / row["source"], base / row["target"]
            entry["backup"] = _backup(project_id, operation_id, row)
            entry["phase"] = "backup_verified"
            _write(path, record)
            _fresh(conn, project_id, root, record, row)
            _verify_backup(project_id, row, entry["backup"])
            if _hash(target, row["target_metadata"]) != row["sha256"]:
                raise CowRefusal("cow_target_preimage_drift")
            stage = target.parent / (".snapshot-cow-" + uuid.uuid4().hex)
            entry["stage_name"] = stage.name
            pair_before = shutil.disk_usage(base).free
            _clone(source, stage)
            _preserve(target, stage, row["target_metadata"])
            _fsync(stage)
            staged = _metadata(stage)
            if _hash(stage, staged) != row["sha256"] or staged["ino"] in (
                    row["source_metadata"]["ino"], row["target_metadata"]["ino"]):
                raise CowRefusal("cow_clone_readback_failed")
            entry["staged_fingerprint"] = staged
            entry["phase"] = "before_replace"
            _write(path, record)
            # Final live/custody/writer checks after staging and durable intent.
            # Staging necessarily changed target-parent mtime, which is restored
            # first and verified again; all inode/ACL/xattrs remain anchored.
            _directories(base, row["directories"], restore=True)
            _fresh(conn, project_id, root, record, row)
            if _hash(target, row["target_metadata"]) != row["sha256"]:
                raise CowRefusal("cow_target_drift_before_rename")
            os.replace(stage, target)
            entry["replacement"] = _metadata(target)
            entry["phase"] = "after_replace"
            _fsync(target.parent, directory=True)
            _write(path, record)
            _directories(base, row["directories"], restore=True)
            _completed_readback(project_id, entry)
            entry["phase"] = "complete"
            entry["filesystem"] = {"available_before": pair_before,
                                    "available_after": shutil.disk_usage(base).free}
            entry["filesystem"]["observed_delta"] = entry["filesystem"]["available_after"] - pair_before
            _write(path, record)
            _write(_state_root(project_id) / (row["candidate_id"] + ".complete.json"),
                   {"custody": custody, "operation_id": operation_id, "entry": entry})
        for entry in record["entries"]:
            _completed_readback(project_id, entry)
        after = shutil.disk_usage(snapshots._snapshot_root(project_id, rows[0]["snapshot_id"])).free
        record["filesystem"].update(available_after=after, observed_net_delta=after - before)
        record["state"] = "complete"  # Data safety is independent of positive df.
        _write(path, record)
    except Exception as exc:
        record["state"] = "partial_or_ambiguous"
        record["error"] = type(exc).__name__ + ":" + str(exc)[:160]
        try:
            _write(path, record)
        except Exception:
            # Never remove pending journals or stages after uncertain persistence.
            record["journal_write_failed"] = True
        result = _result(record)
        result["journal_write_failed"] = record.get("journal_write_failed", False)
        return result
    return _result(record)


@_metadata_scope
def recover(conn: sqlite3.Connection, project_id: str, root: Path, *, operation_id: str,
            action: str = "inspect") -> dict[str, Any]:
    if action not in {"inspect", "restore"}:
        raise CowRefusal("cow_recovery_action_invalid")
    custody = _custody(conn, project_id, root)
    path = _journal_path(project_id, operation_id)
    pending = path.with_name(path.name + ".pending")
    record = _operation(path if path.exists() else pending, project_id)
    if (record.get("schema") != "snapshot_cow_operation.v1" or record.get("custody") != custody
            or record.get("operation_id") != operation_id):
        raise CowRefusal("cow_recovery_scope_mismatch")
    # Inspect the durable intent and pending phase without changing either.
    result = {**_result(record), "mode": "inspect", "writes_performed": False,
              "write_disposition": "not_written", "pending_journal": pending.exists()}
    recovery_path = path.with_name(operation_id + ".recovery.json")
    recovery_pending = recovery_path.with_name(recovery_path.name + ".pending")
    result["recovery_journal"] = _read(recovery_path) if recovery_path.exists() else None
    result["recovery_pending"] = _read(recovery_pending) if recovery_pending.exists() else None
    if action == "inspect":
        return result
    if sys.platform != "darwin" or pending.exists():
        raise CowRefusal("cow_recovery_pending_or_unsupported")
    if recovery_path.exists() or recovery_path.with_name(recovery_path.name + ".pending").exists():
        raise CowRefusal("cow_recovery_journal_exists_inspect_required")
    recovery = {"schema": "snapshot_cow_recovery.v1", "operation_id": operation_id,
                "original_journal_hash": _digest(record), "entries": [], "state": "running"}
    # Preflight every target before writing a recovery receipt or overwriting.
    selected = []
    for entry in record["entries"]:
        row = entry["candidate"]
        _fresh(conn, project_id, root, record, row, recovery=True)
        base = snapshots._snapshot_root(project_id, row["snapshot_id"])
        target = base / row["target"]
        actual = _metadata(target)
        if actual == row["target_metadata"]:
            stage_name = entry.get("stage_name")
            if stage_name and (target.parent / stage_name).exists():
                raise CowRefusal("cow_recovery_retained_stage_inspect_required")
            continue
        known = entry.get("replacement")
        if known is None and entry.get("staged_fingerprint"):
            # Rename changes ctime. An interrupted post-rename target is known
            # only when inode/birth/content and every preserved field match.
            staged = entry["staged_fingerprint"]
            if all(actual[k] == staged[k] for k in staged if k != "ctime_ns"):
                known = actual
        if known is None or actual != known or _hash(target, actual) != row["sha256"]:
            raise CowRefusal("cow_recovery_unrelated_or_unknown_target")
        backup = _verify_backup(project_id, row, entry["backup"])
        selected.append((entry, actual, backup))
    if not selected:
        return {"ok": True, "mode": "restore", "operation_id": operation_id,
                "restored_count": 0, "state": "original_noop", "writes_performed": False,
                "write_disposition": "not_written", "backup_retained": True, "safe_retry": False}
    try:
        _write(recovery_path, recovery)
        for entry, before, backup in selected:
            row = entry["candidate"]
            _fresh(conn, project_id, root, record, row, recovery=True)
            _verify_backup(project_id, row, entry["backup"])
            base = snapshots._snapshot_root(project_id, row["snapshot_id"])
            target = base / row["target"]
            stage = target.parent / (".snapshot-cow-restore-" + uuid.uuid4().hex)
            if _metadata(target) != before:
                raise CowRefusal("cow_recovery_target_race")
            staged = _copy(backup, stage, row["target_metadata"], row["sha256"])
            recovered = {"candidate_id": row["candidate_id"], "phase": "before_replace",
                         "before": before, "staged_fingerprint": staged}
            recovery["entries"].append(recovered)
            _write(recovery_path, recovery)
            _directories(base, row["directories"], restore=True)
            _fresh(conn, project_id, root, record, row)
            if _metadata(target) != before:
                raise CowRefusal("cow_recovery_final_target_race")
            os.replace(stage, target)
            _fsync(target.parent, directory=True)
            actual = _metadata(target)
            recovered.update(phase="after_replace", restored_fingerprint=actual)
            _write(recovery_path, recovery)
            _directories(base, row["directories"], restore=True)
            if (any(actual[k] != row["target_metadata"][k] for k in PRESERVED)
                    or _hash(target, actual) != row["sha256"]
                    or _hash(base / row["source"], row["source_metadata"]) != row["sha256"]):
                raise CowRefusal("cow_recovery_readback_failed")
            recovered["phase"] = "complete"
            _write(recovery_path, recovery)
        recovery["state"] = "complete"
        _write(recovery_path, recovery)
    except Exception as exc:
        recovery["state"] = "partial_or_ambiguous"
        recovery["error"] = type(exc).__name__ + ":" + str(exc)[:160]
        try:
            _write(recovery_path, recovery)
        except Exception:
            pass
    return {"ok": recovery["state"] == "complete", "mode": "restore", "operation_id": operation_id,
            "restored_count": sum(e["phase"] == "complete" for e in recovery["entries"]),
            "state": recovery["state"], "error": recovery.get("error"),
            "writes_performed": bool(recovery["entries"]) or recovery_path.exists() or
            recovery_path.with_name(recovery_path.name + ".pending").exists(), "write_disposition":
            "ambiguous" if recovery["state"] != "complete" else "written" if selected else "not_written",
            "backup_retained": True, "original_journal_retained": True, "safe_retry": False}


@_metadata_scope
def periodic_resolution(conn: sqlite3.Connection, project_id: str, root: Path, *,
                        operation_id: str) -> dict[str, Any]:
    """Read-only terminal proof under the same native facade locks as recovery."""
    custody = _custody(conn, project_id, root)
    path = _journal_path(project_id, operation_id)
    recovery_path = path.with_name(operation_id + '.recovery.json')
    if any(p.with_name(p.name + '.pending').exists() for p in (path, recovery_path)):
        raise CowRefusal('cow_resolution_pending')
    record = _operation(path, project_id)
    if record.get('custody') != custody or record.get('operation_id') != operation_id:
        raise CowRefusal('cow_resolution_scope_drift')
    recovery = _read(recovery_path) if recovery_path.exists() else None
    recovered = {}
    if recovery is not None:
        if (recovery.get('schema') != 'snapshot_cow_recovery.v1'
                or recovery.get('operation_id') != operation_id
                or recovery.get('original_journal_hash') != _digest(record)
                or recovery.get('state') != 'complete'
                or not isinstance(recovery.get('entries'), list)
                or len(recovery['entries']) > len(record['entries'])):
            raise CowRefusal('cow_resolution_recovery_incomplete')
        for entry in recovery['entries']:
            cid = entry.get('candidate_id')
            if cid not in record['candidate_ids'] or cid in recovered or entry.get('phase') != 'complete':
                raise CowRefusal('cow_resolution_recovery_coverage')
            recovered[cid] = entry
    for entry in record['entries']:
        row = entry['candidate']
        _fresh(conn, project_id, root, record, row)
        base = snapshots._snapshot_root(project_id, row['snapshot_id'])
        # An unjournaled stage can never be waved away by a terminal label.
        for relative in row['directories']:
            with os.scandir(_path(base / relative)) as inventory:
                for index, item in enumerate(inventory):
                    if index >= 1000 or item.name.startswith('.snapshot-cow-'):
                        raise CowRefusal('cow_resolution_unknown_stage')
        target = base / row['target']
        if recovery is None and record.get('state') == 'complete':
            if entry.get('phase') != 'complete':
                raise CowRefusal('cow_resolution_complete_phase_invalid')
            _completed_readback(project_id, entry)
        else:
            restored = recovered.get(row['candidate_id'])
            expected = restored.get('restored_fingerprint') if restored else row['target_metadata']
            if (not isinstance(expected, dict) or _metadata(target) != expected
                    or any(expected[k] != row['target_metadata'][k] for k in PRESERVED)
                    or _hash(target, expected) != row['sha256']):
                raise CowRefusal('cow_resolution_target_drift')
    resolution = ('restored' if recovery is not None else 'complete'
                  if record.get('state') == 'complete' else 'original_noop')
    return {'ok': True, 'operation_id': operation_id, 'custody': custody,
            'plan_hash': record.get('plan_hash'), 'config_hash': record.get('config_hash'),
            'run_selection': record.get('run_selection'),
            'original_journal_hash': _digest(record),
            'recovery_journal_hash': _digest(recovery) if recovery is not None else None,
            'candidate_ids': record['candidate_ids'],
            'snapshot_ids': sorted({e['candidate']['snapshot_id'] for e in record['entries']}),
            'resolution': resolution}


@_metadata_scope
def periodic_journal_readiness(conn: sqlite3.Connection, project_id: str, root: Path, *,
                               acknowledgements: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fixed, metadata-only inventory. Unknown suffixes are never an empty census."""
    custody = _custody(conn, project_id, root)
    directory = _state_root(project_id)
    if not directory.exists():
        if acknowledgements:
            raise CowRefusal('cow_resolution_ack_without_journal')
        return {'ready': True, 'skipped_snapshot_ids': [], 'skipped_candidate_ids': []}
    names = set()
    with os.scandir(_path(directory)) as inventory:
        for index, item in enumerate(inventory):
            if index >= 1000:
                raise CowRefusal('cow_journal_inventory_truncated')
            if item.is_symlink() or not item.is_file(follow_symlinks=False):
                raise CowRefusal('cow_journal_inventory_unknown')
            if not re.fullmatch(r'(cowop-[0-9a-f]{32}(?:\.recovery)?\.json(?:\.pending)?|cow-[0-9a-f]{24}\.complete\.json)', item.name):
                raise CowRefusal('cow_journal_inventory_unknown')
            names.add(item.name)
    if any(name.endswith('.pending') for name in names):
        raise CowRefusal('cow_journal_pending_inspect_required')
    originals = {name[:-5] for name in names if re.fullmatch(r'cowop-[0-9a-f]{32}\.json', name)}
    if any(name[:-14] not in originals for name in names if name.endswith('.recovery.json')):
        raise CowRefusal('cow_recovery_original_missing')
    acknowledgements = acknowledgements or {}
    if set(acknowledgements) - originals:
        raise CowRefusal('cow_resolution_ack_without_journal')
    skipped_snapshots, skipped_candidates = set(), set()
    for operation_id in sorted(originals):
        record = _operation(directory / (operation_id + '.json'), project_id)
        if record.get('custody') != custody or record.get('operation_id') != operation_id:
            raise CowRefusal('cow_journal_custody_drift')
        recovery_path = directory / (operation_id + '.recovery.json')
        recovery = _read(recovery_path) if recovery_path.exists() else None
        ack = acknowledgements.get(operation_id)
        if ack is not None:
            proof = ack.get('proof', {})
            ids = sorted({e['candidate']['snapshot_id'] for e in record['entries']})
            if (ack.get('schema') != 'snapshot_cow_periodic_release.v1'
                    or ack.get('custody') != custody or proof.get('custody') != custody
                    or proof.get('operation_id') != operation_id
                    or proof.get('plan_hash') != record.get('plan_hash')
                    or proof.get('config_hash') != record.get('config_hash')
                    or proof.get('run_selection') != record.get('run_selection')
                    or proof.get('original_journal_hash') != _digest(record)
                    or proof.get('recovery_journal_hash') != (_digest(recovery) if recovery is not None else None)
                    or proof.get('candidate_ids') != record['candidate_ids']
                    or proof.get('snapshot_ids') != ids
                    or proof.get('resolution') not in {'complete', 'restored', 'original_noop'}
                    or not re.fullmatch(r'[0-9a-f]{64}', str(ack.get('held_intent_hash', '')))
                    or type(ack.get('scheduler_revision')) is not int
                    or type(ack.get('policy_revision')) is not int
                    or not re.fullmatch(r'[0-9a-f]{64}', str(ack.get('operator_principal_hash', '')))):
                raise CowRefusal('cow_resolution_ack_invalid')
            skipped_snapshots.update(ids)
            skipped_candidates.update(record['candidate_ids'])
        elif (recovery is not None or record.get('state') != 'complete'
              or any(e.get('phase') != 'complete' for e in record['entries'])):
            raise CowRefusal('cow_operation_inspect_required')
    return {'ready': True, 'skipped_snapshot_ids': sorted(skipped_snapshots),
            'skipped_candidate_ids': sorted(skipped_candidates)}


@_metadata_scope
def periodic_zero_effect(conn: sqlite3.Connection, project_id: str, root: Path, *,
                         plan: dict[str, Any], acknowledgements: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Known pre-journal refusal must also prove unchanged files under native locks."""
    path = _journal_path(project_id, plan['operation_id'])
    if path.exists() or path.with_name(path.name + '.pending').exists():
        raise CowRefusal('cow_zero_effect_journal_present')
    periodic_journal_readiness(conn, project_id, root, acknowledgements=acknowledgements)
    for row in plan['candidates']:
        _fresh(conn, project_id, root, plan, row)
        base = snapshots._snapshot_root(project_id, row['snapshot_id'])
        if _hash(base / row['target'], row['target_metadata']) != row['sha256']:
            raise CowRefusal('cow_zero_effect_target_drift')
        with os.scandir(_path(base / Path(row['target']).parent)) as inventory:
            for index, item in enumerate(inventory):
                if index >= 1000 or item.name.startswith('.snapshot-cow-'):
                    raise CowRefusal('cow_zero_effect_stage_unknown')
    return {'ok': True, 'zero_effect_verified': True}
