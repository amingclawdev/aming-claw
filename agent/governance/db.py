"""SQLite database layer for governance runtime state.

Manages:
  - Connection lifecycle (per-project databases)
  - Schema creation and migration
  - WAL mode for concurrent read/write
"""

import os
import sys
import sqlite3
import stat
import threading
import hashlib
import json
import re
import fcntl
import subprocess
import errno
import math
import struct
import urllib.request
import urllib.error
import urllib.parse
import shutil
import tempfile
from contextlib import closing
from pathlib import Path
from collections.abc import Mapping, Sequence

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from utils import tasks_root


SCHEMA_VERSION = 47

AC_PROJECT_ID = "aming-claw"
DEV_RUNTIME_PLANE = "dev"
RUNTIME_PLANE_ENV = "AMING_CLAW_RUNTIME_PLANE"
AC_DEV_STORAGE_ROOT_ENV = "AMING_CLAW_DEV_STORAGE_ROOT"
AC_STABLE_SHARED_VOLUME_ENV = "AMING_CLAW_SHARED_VOLUME"
AC_DEV_WORLD_ID = "ac-dev"
AC_STABLE_WORLD_ID = "ac-stable"
AC_WORLD_GENESIS_SCHEMA = "ac_governance_world_genesis.v1"

AC_DATABASE_STABLE_RELATIVE_PATH = (
    "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
)
AC_DATABASE_DEV_RELATIVE_PATH = "governance/aming-claw/governance.db"
AC_LEGACY_ARCHIVE_SIZE_BYTES = 178_625_794_048
AC_DEV_CUTOVER_SCHEMA = "ac_dev_world_cutover.v1"
AC_DEV_LAUNCH_RECEIPT_SCHEMA = "ac_dev_launch_receipt.v1"
AC_DEV_LAUNCH_RECEIPT_NAME = "launch-receipt.json"
AC_DEV_COW_SUCCESSOR_SCHEMA = "ac_dev_cow_database_successor.v2"
AC_DEV_COW_SUCCESSOR_ARCHIVE = "archive/cow-database-successor"
AC_DEV_COW_SUCCESSOR_PREFIX = "successor-v2"

_SQLITE_WRITE_LOCK = threading.RLock()
_DEV_DATABASE_WRITER_LEASES: dict[str, dict[str, object]] = {}
_DEV_DATABASE_WRITER_LEASES_LOCK = threading.RLock()


def _sqlite_quote_identifier(identifier: str) -> str:
    """Return one deterministic SQLite double-quoted identifier."""
    if not isinstance(identifier, str):
        raise TypeError("SQLite identifier must be text")
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_projection_value(value: object) -> list[str]:
    """Encode one SQLite value without type, sign, or byte ambiguity."""
    if value is None:
        return ["null", ""]
    # sqlite3 returns INTEGER as int.  Treat an injected Python bool by the
    # same SQLite storage-class policy instead of inventing a BOOLEAN class.
    if isinstance(value, bool):
        return ["integer", "1" if value else "0"]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            return ["float", "nan"]
        if math.isinf(value):
            return ["float", "+inf" if value > 0 else "-inf"]
        # IEEE-754 bytes are exact and preserve -0.0 independently of +0.0.
        return ["float64-be", struct.pack(">d", value).hex()]
    if isinstance(value, str):
        # JSON's UTF-8 encoding is deterministic; the tag separates TEXT from BLOB.
        return ["text-utf8", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["blob-hex", bytes(value).hex()]
    raise TypeError("unsupported SQLite projection value type: " + type(value).__name__)


def _sqlite_logical_projection(
    connection: sqlite3.Connection, *, exclude_tables: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Hash ordered tables/columns/rows with one lossless tagged encoding."""
    tables = [str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) if str(row[0]) not in exclude_tables and not str(row[0]).startswith("sqlite_")]
    projection: dict[str, str] = {}
    for table in tables:
        quoted_table = _sqlite_quote_identifier(table)
        columns = [str(row[1]) for row in connection.execute(
            f"PRAGMA table_info({quoted_table})"
        )]
        quoted_columns = ",".join(_sqlite_quote_identifier(column) for column in columns)
        rows = connection.execute(
            f"SELECT {quoted_columns} FROM {quoted_table}"
        ).fetchall()
        encoded_rows = [[_sqlite_projection_value(value) for value in row] for row in rows]
        encoded_rows.sort(key=lambda row: json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8"))
        payload = {
            "table": table,
            "columns": columns,
            "rows": encoded_rows,
        }
        projection[table] = "sha256:" + hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
    return projection
def _stable_health_request() -> dict[str, object]:
    """Fixed read-only localhost stable health boundary."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:40000/api/health", timeout=2) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("AC stable authority is unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AC stable authority health is invalid")
    return payload


def _stable_process_identity(pid: int) -> tuple[str, str, str]:
    """Read start, argv and cwd from the OS; a PID alone is never authority."""
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("AC stable authority PID is invalid")
    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        # macOS does not expose procfs.  ``lsof`` is the OS-owned equivalent
        # for a process's current directory; do not weaken this to a PID-only
        # check when procfs is absent.
        try:
            lsof = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            cwd = next(
                (line[1:] for line in lsof.stdout.splitlines() if line.startswith("n")),
                "",
            )
        except (OSError, subprocess.SubprocessError):
            cwd = ""
    if not start or not command or not cwd:
        raise RuntimeError("AC stable authority process identity is unavailable")
    return start, command, cwd


def _verified_stable_binding() -> dict[str, object]:
    """Read-only fixed-40000 + unique stable-worktree authority for AC dev."""
    health = _stable_health_request()
    if not isinstance(health, Mapping) or not (
        health.get("status") == "ok" and health.get("service") == "governance"
        and health.get("port") == 40000 and health.get("runtime_plane") == "stable"
        and health.get("runtime_stale") is False
        and isinstance(health.get("pid"), int) and health["pid"] > 0
    ):
        raise RuntimeError("AC stable authority health is invalid")
    _start, command, cwd = _stable_process_identity(int(health["pid"]))
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5, check=False)
    roots = []
    for block in result.stdout.strip().split("\n\n"):
        fields = dict(line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines())
        if fields.get("branch") == "refs/heads/codex/direct-no-pass-post-reconcile-r2" and fields.get("worktree"):
            roots.append(Path(fields["worktree"]).resolve(strict=True))
    if len(roots) != 1:
        raise RuntimeError("AC stable authority worktree is unavailable")
    stable_root = roots[0]
    server_command = "agent.governance.server" in command or (
        "-m agent.cli" in command and " start " in f" {command} "
    )
    if (
        not _start
        or Path(cwd).resolve(strict=True) != stable_root
        or not server_command
    ):
        raise RuntimeError("AC stable authority process binding is invalid")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=stable_root, capture_output=True, text=True, timeout=5, check=False).stdout.strip().lower()
    source_path = stable_root / "agent" / "governance" / "server.py"
    source_hash = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
    identity = health.get("runtime_plane_identity") if isinstance(health.get("runtime_plane_identity"), Mapping) else {}
    loaded = health.get("loaded_runtime_identity") if isinstance(health.get("loaded_runtime_identity"), Mapping) else {}
    if not (
        health.get("runtime_loaded_version") == head
        and identity.get("worktree_root") == str(stable_root)
        and identity.get("branch") == "codex/direct-no-pass-post-reconcile-r2"
        and identity.get("commit") == head
        and identity.get("stable_anchor_commit") == head
        and loaded.get("loaded_commit") == head
        and loaded.get("loaded_source_path") == str(source_path)
        and loaded.get("loaded_source_sha256") == source_hash
        and loaded.get("worktree_source_sha256") == source_hash
        and "aming-claw" not in set(identity.get("project_allowlist") or [])
    ):
        raise RuntimeError("AC stable authority source identity is invalid")
    shared = roots[0] / "shared-volume"
    if not shared.is_dir() or shared.is_symlink() or shared.resolve(strict=True) != shared:
        raise RuntimeError("AC stable authority shared volume is invalid")
    return {"shared_volume_path": str(shared), "health": dict(health), "stable_head": head}


def verified_stable_database_binding(
    requested_shared_volume: str | None = None,
    *,
    stable_anchor_commit: str | None = None,
) -> dict[str, object]:
    """Return the one verified stable DB binding, with TOCTOU revalidation data."""
    binding = _verified_stable_binding()
    shared = _absolute_non_symlink_root(
        Path(str(binding["shared_volume_path"])), create=False
    )
    if requested_shared_volume is not None:
        requested = _absolute_non_symlink_root(
            Path(requested_shared_volume), create=False
        )
        if requested != shared:
            raise RuntimeError("AC dev runtime requires the exact stable-worktree shared volume")
    head = str(binding["stable_head"])
    if stable_anchor_commit is not None and str(stable_anchor_commit).lower() != head:
        raise RuntimeError("stable service authority changed while binding its database")
    database = (shared / Path(AC_DATABASE_STABLE_RELATIVE_PATH).relative_to("shared-volume")).absolute()
    before = database.stat(follow_symlinks=False)
    if (
        database.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or database.resolve(strict=True) != database
    ):
        raise RuntimeError("AC dev runtime canonical stable database identity is invalid")
    identity = {
        "schema_version": "ac_stable_database_identity.v1",
        "device": int(before.st_dev),
        "inode": int(before.st_ino),
        "stable_relative_path_sha256": "sha256:" + hashlib.sha256(
            AC_DATABASE_STABLE_RELATIVE_PATH.encode("utf-8")
        ).hexdigest(),
    }
    health_identity = dict(binding["health"].get("runtime_plane_identity") or {})
    if (
        health_identity.get("database_identity") != identity
        or health_identity.get("stable_database_identity") != identity
    ):
        raise RuntimeError("stable service database identity differs from the stable worktree")
    after = database.stat(follow_symlinks=False)
    if (
        database.is_symlink()
        or not stat.S_ISREG(after.st_mode)
        or (int(after.st_dev), int(after.st_ino)) != (int(before.st_dev), int(before.st_ino))
    ):
        raise RuntimeError("stable database identity changed while binding")
    return {
        **binding,
        "shared_volume_path": str(shared),
        "database_path": str(database),
        "stable_database_identity": identity,
    }


def _revalidate_stable_database_binding(binding: Mapping[str, object]) -> None:
    """Fail before a dev root, receipt, or DB effect if the stable DB was swapped."""
    database = Path(str(binding.get("database_path") or ""))
    expected = binding.get("stable_database_identity")
    if not database or not isinstance(expected, Mapping):
        raise RuntimeError("stable database binding is incomplete")
    metadata = database.stat(follow_symlinks=False)
    actual = {
        "schema_version": "ac_stable_database_identity.v1",
        "device": int(metadata.st_dev), "inode": int(metadata.st_ino),
        "stable_relative_path_sha256": "sha256:" + hashlib.sha256(
            AC_DATABASE_STABLE_RELATIVE_PATH.encode("utf-8")
        ).hexdigest(),
    }
    if database.is_symlink() or not stat.S_ISREG(metadata.st_mode) or actual != dict(expected):
        raise RuntimeError("stable database identity changed before dev effect")


def _connection_main_database_identity(
    conn: sqlite3.Connection,
) -> tuple[Path, os.stat_result] | None:
    """Read the exact physical main SQLite file already opened by ``conn``."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_paths = [
            str(row[2] or "")
            for row in rows
            if str(row[1] or "") == "main"
        ]
    except sqlite3.Error:
        return None
    if len(main_paths) != 1 or not main_paths[0]:
        return None
    candidate = Path(main_paths[0])
    try:
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or not candidate.is_file()
            or candidate.resolve(strict=True) != candidate
        ):
            return None
        metadata = candidate.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return candidate, metadata


def _unknown_graph_activation_connection(reason: str) -> dict[str, object]:
    from agent.runtime_plane import graph_activation_policy

    return {**graph_activation_policy("unknown"), "classification_reason": reason}


def classify_graph_activation_connection(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Classify an opened graph DB without accepting caller/environment plane claims.

    Active graph truth is allowed only when this *opened connection* is the
    exact live stable database.  A dev connection is recognized from the
    canonical external root, receipt, and genesis invariants and is denied.
    Everything else is ``unknown`` and denied before a graph ref, event, or
    projection can be written.
    """
    from agent.runtime_plane import graph_activation_policy, resolve_ac_dev_storage_root

    opened = _connection_main_database_identity(conn)
    if opened is None:
        return _unknown_graph_activation_connection("main_database_identity_unavailable")
    database, before = opened
    try:
        binding = verified_stable_database_binding()
        stable_database = Path(str(binding["database_path"])).absolute()
        stable_identity = dict(binding["stable_database_identity"])
        if (
            database == stable_database
            and int(before.st_dev) == int(stable_identity["device"])
            and int(before.st_ino) == int(stable_identity["inode"])
        ):
            _revalidate_stable_database_binding(binding)
            after = stable_database.stat(follow_symlinks=False)
            if (
                not stable_database.is_symlink()
                and stat.S_ISREG(after.st_mode)
                and (int(after.st_dev), int(after.st_ino))
                == (int(before.st_dev), int(before.st_ino))
            ):
                return {
                    **graph_activation_policy("stable"),
                    "classification_reason": "verified_stable_database_binding",
                }
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error):
        # A stable verification failure cannot be rescued by a claimed plane.
        pass

    try:
        # This derives the dev root from live stable authority, rather than
        # trusting AMING_CLAW_DEV_STORAGE_ROOT or a store caller.
        binding = verified_stable_database_binding()
        stable = Path(str(binding["shared_volume_path"])).absolute()
        root = resolve_ac_dev_storage_root(stable)
        expected_database = (root / AC_DATABASE_DEV_RELATIVE_PATH).absolute()
        if database != expected_database:
            return _unknown_graph_activation_connection("main_database_not_canonical_world")
        root_meta = root.stat(follow_symlinks=False)
        if root.is_symlink() or root.resolve(strict=True) != root:
            return _unknown_graph_activation_connection("dev_storage_root_identity_invalid")
        receipt_path = root / AC_DEV_LAUNCH_RECEIPT_NAME
        if receipt_path.is_symlink() or not receipt_path.is_file():
            return _unknown_graph_activation_connection("dev_launch_receipt_missing")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        stable_meta = stable.stat(follow_symlinks=False)
        stable_parent_meta = stable.parent.stat(follow_symlinks=False)
        server_source = Path(__file__).with_name("server.py")
        source_sha256 = "sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest()
        required_receipt = {
            "schema_version": AC_DEV_LAUNCH_RECEIPT_SCHEMA,
            "world_id": AC_DEV_WORLD_ID,
            "project_id": AC_PROJECT_ID,
            "runtime_plane": DEV_RUNTIME_PLANE,
            "port": 40008,
            "background": False,
            "storage_root": str(root),
            "storage_device": int(root_meta.st_dev),
            "storage_inode": int(root_meta.st_ino),
            "stable_shared_volume": str(stable),
            "stable_shared_volume_device": int(stable_meta.st_dev),
            "stable_shared_volume_inode": int(stable_meta.st_ino),
            "stable_parent_device": int(stable_parent_meta.st_dev),
            "stable_parent_inode": int(stable_parent_meta.st_ino),
            "source_sha256": source_sha256,
        }
        if not isinstance(receipt, Mapping) or any(
            receipt.get(key) != value for key, value in required_receipt.items()
        ):
            return _unknown_graph_activation_connection("dev_launch_receipt_mismatch")
        meta = dict(conn.execute("SELECT key, value FROM schema_meta"))
        genesis = json.loads(str(meta.get("governance_world_genesis_json") or ""))
        genesis_hash = str(meta.get("governance_world_genesis_sha256") or "")
        expected_genesis_hash = _world_genesis_hash(genesis)
        database_identity = dict(genesis.get("database_identity") or {})
        storage_identity = dict(genesis.get("storage_root_identity") or {})
        if not (
            meta.get("governance_world_id") == AC_DEV_WORLD_ID
            and genesis_hash == expected_genesis_hash
            and genesis.get("schema_version") == AC_WORLD_GENESIS_SCHEMA
            and genesis.get("world_id") == AC_DEV_WORLD_ID
            and genesis.get("project_id") == AC_PROJECT_ID
            and genesis.get("source_only") is True
            and genesis.get("rows_copied") == 0
            and database_identity.get("device") == int(before.st_dev)
            and database_identity.get("inode") == int(before.st_ino)
            and storage_identity.get("path") == str(root)
            and storage_identity.get("device") == int(root_meta.st_dev)
            and storage_identity.get("inode") == int(root_meta.st_ino)
        ):
            return _unknown_graph_activation_connection("dev_genesis_identity_invalid")
        _revalidate_stable_database_binding(binding)
        after = expected_database.stat(follow_symlinks=False)
        if (
            expected_database.is_symlink()
            or not stat.S_ISREG(after.st_mode)
            or (int(after.st_dev), int(after.st_ino))
            != (int(before.st_dev), int(before.st_ino))
        ):
            return _unknown_graph_activation_connection("dev_database_identity_changed")
        return {
            **graph_activation_policy("dev"),
            "classification_reason": "verified_dev_root_receipt_genesis",
        }
    except (json.JSONDecodeError, KeyError, OSError, RuntimeError, ValueError, sqlite3.Error):
        return _unknown_graph_activation_connection("dev_database_binding_unverified")

_DEV_DENIED_SCHEMA_ACTIONS = frozenset(
    code
    for name in (
        "SQLITE_ALTER_TABLE",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DETACH",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_DROP_VTABLE",
        "SQLITE_REINDEX",
    )
    if isinstance((code := getattr(sqlite3, name, None)), int)
)


# The dev service is deliberately denied migration authority, so a table name
# and a handful of column names are not a sufficient capability check.  These
# are the write-critical identity constraints for every table inspected by a
# dev-plane lazy-schema owner.  Required columns not listed as INTEGER are
# canonical TEXT columns; every required non-single-column-PK column is NOT
# NULL.  Keeping this contract here gives all lazy owners one fail-closed
# interpretation of PRAGMA metadata, including SQLite autoindexes.
_DEV_SCHEMA_INTEGER_COLUMNS: Mapping[str, frozenset[str]] = {
    "contract_runtime_executions": frozenset(
        {"execution_state_revision"}
    ),
    "worker_implementation_test_results_corrections": frozenset(
        {"source_completed_line_index", "source_execution_state_revision"}
    ),
    "backlog_contract_chain_bindings": frozenset(
        {"id", "generation", "execution_state_revision"}
    ),
    "contract_chain_edges": frozenset({"id", "generation"}),
    "backlog_contract_chain_current": frozenset(
        {"generation", "projection_watermark"}
    ),
    "task_timeline_events": frozenset(
        {
            "id",
            "attempt_num",
            "parent_event_id",
            "schema_version",
        }
    ),
    "parallel_branch_runtime_contexts": frozenset(
        {"attempt", "retry_round"}
    ),
    "parallel_branch_batch_items": frozenset({"queue_index", "retained"}),
    "parallel_branch_merge_queue_items": frozenset(
        {"queue_index", "validation_attempt"}
    ),
    "parallel_branch_integration_epochs": frozenset({"merge_cursor"}),
    "parallel_branch_integration_epoch_worldref_seals": frozenset(
        {"merge_cursor"}
    ),
    "parallel_branch_integration_epoch_release_events": frozenset({"id"}),
    "parallel_branch_integration_epoch_release_rollovers": frozenset(
        {"release_event_id", "sequence"}
    ),
    "parallel_branch_integration_epoch_release_projection_repairs": frozenset(
        {"release_event_id"}
    ),
}

_DEV_SCHEMA_PRIMARY_KEYS: Mapping[str, tuple[str, ...]] = {
    "contract_runtime_executions": ("contract_execution_id",),
    "worker_implementation_test_results_corrections": ("correction_id",),
    "backlog_contract_chain_bindings": ("id",),
    "contract_chain_edges": ("id",),
    "backlog_contract_chain_current": ("project_id", "backlog_id"),
    "task_timeline_events": ("id",),
    "observer_route_token_refs": ("project_id", "route_token_ref"),
    "parallel_branch_runtime_contexts": ("project_id", "task_id"),
    "parallel_branch_batch_runtimes": ("project_id", "batch_id"),
    "parallel_branch_batch_items": ("project_id", "batch_id", "task_id"),
    "parallel_branch_runtime_contract_revisions": (
        "project_id",
        "runtime_context_id",
        "revision_id",
    ),
    "parallel_branch_merge_queue_items": (
        "project_id",
        "merge_queue_id",
        "queue_item_id",
    ),
    "parallel_branch_integration_epochs": ("project_id", "batch_id"),
    "parallel_branch_integration_epoch_world_refs": (
        "project_id",
        "batch_id",
        "epoch_id",
        "world_ref_id",
    ),
    "parallel_branch_integration_epoch_worldref_seals": (
        "project_id",
        "batch_id",
        "epoch_id",
    ),
    "parallel_branch_integration_epoch_release_events": ("id",),
    "parallel_branch_integration_epoch_release_rollovers": (
        "project_id",
        "batch_id",
        "queue_item_id",
        "release_event_id",
        "sequence",
    ),
    "parallel_branch_integration_epoch_release_projection_repairs": (
        "project_id",
        "batch_id",
        "queue_item_id",
        "release_event_id",
    ),
    "parallel_branch_runtime_access_audit": ("audit_id",),
}

_DEV_SCHEMA_UNIQUE_CONSTRAINTS: Mapping[
    str, tuple[tuple[str, ...], ...]
] = {
    "worker_implementation_test_results_corrections": (
        (
            "project_id",
            "contract_execution_id",
            "runtime_context_id",
            "task_id",
            "source_completed_line_index",
            "source_line_sha256",
        ),
    ),
    "backlog_contract_chain_bindings": (("idempotency_key",),),
    "contract_chain_edges": (
        ("edge_key",),
        (
            "project_id",
            "contract_chain_id",
            "parent_contract_execution_id",
            "child_contract_execution_id",
            "edge_kind",
        ),
    ),
    "parallel_branch_integration_epoch_world_refs": (
        ("project_id", "batch_id", "epoch_id", "world_kind", "commit_sha"),
    ),
    "parallel_branch_integration_epoch_worldref_seals": (
        ("seal_id",),
        ("project_id", "merge_queue_id", "queue_item_id"),
    ),
    "parallel_branch_integration_epoch_release_events": (
        ("project_id", "batch_id", "queue_item_id"),
    ),
    "parallel_branch_integration_epoch_release_rollovers": (
        ("project_id", "batch_id", "queue_item_id", "rollover_id"),
    ),
    "parallel_branch_integration_epoch_release_projection_repairs": (
        ("project_id", "batch_id", "queue_item_id", "repair_id"),
    ),
}


class DevRuntimeSchemaVerificationError(RuntimeError):
    """A dev-plane schema capability is absent or stale.

    The dev service shares the stable database but has no migration authority.
    Callers can use ``code``/``details`` as a typed, zero-write failure without
    parsing SQLite's platform-specific authorization error text.
    """

    code = "ac_dev_verify_only_schema_incompatible"

    def __init__(
        self,
        owner: str,
        *,
        missing_tables: Sequence[str] = (),
        missing_columns: Mapping[str, Sequence[str]] | None = None,
        missing_indexes: Sequence[str] = (),
        invalid_indexes: Mapping[str, Mapping[str, str]] | None = None,
        invalid_columns: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
        missing_unique_constraints: Mapping[str, Sequence[Sequence[str]]] | None = None,
    ) -> None:
        self.details = {
            "schema_version": "ac_dev_verify_only_schema_capability.v1",
            "runtime_plane": DEV_RUNTIME_PLANE,
            "schema_owner": str(owner or "unknown"),
            "missing_tables": sorted(set(missing_tables)),
            "missing_columns": {
                str(table): sorted(set(columns))
                for table, columns in sorted((missing_columns or {}).items())
                if columns
            },
            "missing_indexes": sorted(set(missing_indexes)),
            "invalid_indexes": {
                str(index): {
                    str(key): str(value)
                    for key, value in sorted(details.items())
                }
                for index, details in sorted((invalid_indexes or {}).items())
                if details
            },
            "invalid_columns": {
                str(table): {
                    str(column): {
                        str(key): str(value)
                        for key, value in sorted(details.items())
                    }
                    for column, details in sorted(columns.items())
                    if details
                }
                for table, columns in sorted((invalid_columns or {}).items())
                if columns
            },
            "missing_unique_constraints": {
                str(table): [list(columns) for columns in constraints]
                for table, constraints in sorted(
                    (missing_unique_constraints or {}).items()
                )
                if constraints
            },
            "verify_only": True,
            "ddl_attempted": False,
            "writes_performed": False,
            "migration_allowed": False,
        }
        summary = ", ".join(
            part
            for part in (
                "tables=" + ",".join(self.details["missing_tables"])
                if self.details["missing_tables"]
                else "",
                "columns="
                + ",".join(
                    f"{table}({','.join(columns)})"
                    for table, columns in self.details["missing_columns"].items()
                )
                if self.details["missing_columns"]
                else "",
                "indexes=" + ",".join(self.details["missing_indexes"])
                if self.details["missing_indexes"]
                else "",
                "invalid_indexes="
                + ",".join(self.details["invalid_indexes"])
                if self.details["invalid_indexes"]
                else "",
                "invalid_columns="
                + ",".join(
                    f"{table}({','.join(columns)})"
                    for table, columns in self.details["invalid_columns"].items()
                )
                if self.details["invalid_columns"]
                else "",
                "unique_constraints="
                + ",".join(self.details["missing_unique_constraints"])
                if self.details["missing_unique_constraints"]
                else "",
            )
            if part
        )
        super().__init__(
            f"{self.code}: {self.details['schema_owner']}"
            + (f": {summary}" if summary else "")
        )


def dev_runtime_verify_only() -> bool:
    """Return whether this process must inspect, never migrate, SQLite schema."""

    return _is_dev_runtime()


_GRAPH_MATERIALIZATION_ADMISSION_LOCAL = threading.local()


def graph_materialization_admission_active(conn: sqlite3.Connection) -> bool:
    """Return whether ``conn`` is inside the one bounded graph-schema admission."""

    return id(conn) in getattr(
        _GRAPH_MATERIALIZATION_ADMISSION_LOCAL, "connection_ids", frozenset()
    )


def execute_graph_schema_sql(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a schema script without ``executescript``'s implicit COMMIT.

    This path is used only by the dev materialization admission.  Stable keeps
    the historical ``executescript`` behavior byte-for-byte at each owner.
    """

    statement = ""
    for line in str(sql).splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("graph schema source contains an incomplete statement")


def _graph_materialization_inventory(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    rows = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE type IN ('table','index','trigger','view') ORDER BY type,name,tbl_name"
    ).fetchall()
    return [tuple(str(value or "") for value in row) for row in rows]


def _graph_materialization_canonical_inventory() -> list[tuple[str, str, str, str]]:
    from . import graph_correction_patches, graph_events, graph_snapshot_store

    canonical = sqlite3.connect(":memory:")
    try:
        connection_ids = set(
            getattr(_GRAPH_MATERIALIZATION_ADMISSION_LOCAL, "connection_ids", ())
        )
        connection_ids.add(id(canonical))
        _GRAPH_MATERIALIZATION_ADMISSION_LOCAL.connection_ids = frozenset(connection_ids)
        graph_snapshot_store.ensure_schema(canonical)
        graph_events.ensure_schema(canonical)
        graph_correction_patches.ensure_schema(canonical)
        return _graph_materialization_inventory(canonical)
    finally:
        connection_ids = set(
            getattr(_GRAPH_MATERIALIZATION_ADMISSION_LOCAL, "connection_ids", ())
        )
        connection_ids.discard(id(canonical))
        _GRAPH_MATERIALIZATION_ADMISSION_LOCAL.connection_ids = frozenset(connection_ids)
        canonical.close()


def _graph_materialization_managed_inventory(
    inventory: Sequence[tuple[str, str, str, str]],
    canonical: Sequence[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    names = {row[1] for row in canonical}
    tables = {row[2] for row in canonical if row[0] == "table"}
    return [row for row in inventory if row[1] in names or row[2] in tables]


def verify_graph_materialization_schema(conn: sqlite3.Connection) -> None:
    """Verify the three source-owned rebuildable graph schemas without writes."""

    canonical = _graph_materialization_canonical_inventory()
    actual = _graph_materialization_inventory(conn)
    managed = _graph_materialization_managed_inventory(actual, canonical)
    extra_graph = [
        row for row in actual
        if row[1].startswith("graph_") and row[1] not in {item[1] for item in canonical}
    ]
    if managed != canonical or extra_graph:
        raise DevRuntimeSchemaVerificationError(
            "graph_materialization",
            missing_tables=[
                row[1] for row in canonical
                if row[0] == "table" and row not in managed
            ],
        )


def admit_ac_dev_graph_materialization_schema(
    conn: sqlite3.Connection, *, project_id: str
) -> dict[str, object]:
    """Initialize/verify only the source-owned graph materialization schema.

    Authority is derived from the opened database's physical world identity;
    caller plane claims cannot widen it.  All owner DDL and postcondition
    verification share one ``BEGIN IMMEDIATE`` transaction.
    """

    from . import graph_correction_patches, graph_events, graph_snapshot_store

    if project_id != AC_PROJECT_ID or not _is_dev_runtime():
        raise ValueError("AC dev graph materialization admission is dev/aming-claw only")
    runtime_custody = _require_ac_dev_graph_materialization_runtime_custody(conn)
    database_identity = canonical_ac_database_identity(conn)
    if (
        database_identity.get("world_id") != AC_DEV_WORLD_ID
        or database_identity.get("project_id") != AC_PROJECT_ID
    ):
        raise ValueError("AC dev graph materialization custody identity is not admitted")
    policy = classify_graph_activation_connection(conn)
    if (
        policy.get("runtime_plane") != DEV_RUNTIME_PLANE
        or policy.get("classification_reason") != "verified_dev_root_receipt_genesis"
        or policy.get("active_graph_activation_allowed") is not False
    ):
        raise ValueError("AC dev graph materialization database identity is not admitted")

    canonical = _graph_materialization_canonical_inventory()
    before_inventory = _graph_materialization_inventory(conn)
    before_managed = _graph_materialization_managed_inventory(
        before_inventory, canonical
    )
    canonical_names = {item[1] for item in canonical}
    extra_graph_before = [
        row for row in before_inventory
        if row[1].startswith("graph_") and row[1] not in canonical_names
    ]
    # Admission initializes one absent rebuildable materialization or replays
    # one exact current source inventory.  A partial, altered, extra, or legacy
    # graph layout is not migrated and cannot be laundered by IF NOT EXISTS.
    if extra_graph_before or (before_managed and before_managed != canonical):
        raise ValueError("AC dev graph materialization preimage is not exact or empty")
    connection_ids = set(
        getattr(_GRAPH_MATERIALIZATION_ADMISSION_LOCAL, "connection_ids", ())
    )
    if id(conn) in connection_ids:
        raise RuntimeError("nested graph materialization admission is forbidden")
    prior_authorizer = _dev_schema_authorizer
    try:
        with sqlite_write_lock():
            conn.execute("BEGIN IMMEDIATE")
            conn.set_authorizer(None)
            connection_ids.add(id(conn))
            _GRAPH_MATERIALIZATION_ADMISSION_LOCAL.connection_ids = frozenset(connection_ids)
            graph_snapshot_store.ensure_schema(conn)
            graph_events.ensure_schema(conn)
            graph_correction_patches.ensure_schema(conn)
            actual = _graph_materialization_inventory(conn)
            if _graph_materialization_managed_inventory(actual, canonical) != canonical:
                raise ValueError("AC dev graph materialization schema postcondition failed")
            extra_graph = [
                row for row in actual
                if row[1].startswith("graph_")
                and row[1] not in canonical_names
            ]
            if extra_graph:
                raise ValueError("AC dev graph materialization schema has extra authority")
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        connection_ids.discard(id(conn))
        _GRAPH_MATERIALIZATION_ADMISSION_LOCAL.connection_ids = frozenset(connection_ids)
        conn.set_authorizer(prior_authorizer)
    return {
        "schema_version": "ac_dev_graph_materialization_admission.v1",
        "project_id": project_id,
        "runtime_plane": DEV_RUNTIME_PLANE,
        "world_id": AC_DEV_WORLD_ID,
        "object_count": len(canonical),
        "active_graph_activation_allowed": False,
        "runtime_custody": runtime_custody,
    }


def verify_existing_schema_capabilities(
    conn: sqlite3.Connection,
    *,
    owner: str,
    required_tables: Sequence[str],
    required_columns: Mapping[str, Sequence[str]] | None = None,
    required_indexes: Sequence[str] = (),
    required_index_definitions: Mapping[str, Mapping[str, str]] | None = None,
) -> None:
    """Verify exact existing SQLite capabilities without issuing any DDL.

    This helper is intentionally based only on ``sqlite_master`` and read-only
    ``PRAGMA table_info`` inspection.  It is the common dev-plane counterpart
    to the stable runtime's lazy schema/migration helpers.
    """

    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
    tables = tuple(dict.fromkeys(str(item) for item in required_tables))
    columns = {
        str(table): tuple(dict.fromkeys(str(item) for item in values))
        for table, values in (required_columns or {}).items()
    }
    index_definitions = {
        str(index): {
            "table": str(definition.get("table") or ""),
            "sql": str(definition.get("sql") or ""),
        }
        for index, definition in (required_index_definitions or {}).items()
    }
    indexes = tuple(
        dict.fromkeys(
            [str(item) for item in required_indexes]
            + list(index_definitions)
        )
    )
    for value in (*tables, *columns, *indexes):
        if not identifier.fullmatch(value):
            raise ValueError(f"invalid SQLite schema identifier: {value!r}")
    for values in columns.values():
        for value in values:
            if not identifier.fullmatch(value):
                raise ValueError(f"invalid SQLite schema identifier: {value!r}")
    for definition in index_definitions.values():
        table = definition["table"]
        if not identifier.fullmatch(table):
            raise ValueError(f"invalid SQLite schema identifier: {table!r}")
        if not definition["sql"].strip():
            raise ValueError("required index SQL must be non-empty")

    placeholders = ", ".join("?" for _ in tables)
    present_tables = set()
    if tables:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            tables,
        ).fetchall()
        present_tables = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
            for row in rows
        }
    missing_tables = sorted(set(tables) - present_tables)
    missing_columns: dict[str, list[str]] = {}
    table_info: dict[str, dict[str, dict[str, object]]] = {}
    for table, expected in columns.items():
        if table not in present_tables:
            continue
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        info = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1]): {
                "type": str(
                    row["type"] if isinstance(row, sqlite3.Row) else row[2]
                ).upper(),
                "notnull": int(
                    row["notnull"] if isinstance(row, sqlite3.Row) else row[3]
                ),
                "pk": int(row["pk"] if isinstance(row, sqlite3.Row) else row[5]),
            }
            for row in rows
        }
        table_info[table] = info
        present = set(info)
        missing = sorted(set(expected) - present)
        if missing:
            missing_columns[table] = missing

    invalid_columns: dict[str, dict[str, dict[str, str]]] = {}
    for table, expected in columns.items():
        info = table_info.get(table, {})
        primary_key = _DEV_SCHEMA_PRIMARY_KEYS.get(table, ())
        integer_columns = _DEV_SCHEMA_INTEGER_COLUMNS.get(table, frozenset())
        for column in expected:
            actual = info.get(column)
            if actual is None:
                continue
            expected_type = "INTEGER" if column in integer_columns else "TEXT"
            expected_pk = (
                primary_key.index(column) + 1 if column in primary_key else 0
            )
            # SQLite reports NOT NULL=0 for a single-column ``PRIMARY KEY``
            # declaration even though the key is unique and non-null in the
            # write model.  Composite PK members are reported NOT NULL=1.
            expected_notnull = int(not (len(primary_key) == 1 and expected_pk))
            mismatch = {}
            for key, expected_value in (
                ("type", expected_type),
                ("notnull", expected_notnull),
                ("pk", expected_pk),
            ):
                actual_value = actual[key]
                if actual_value != expected_value:
                    mismatch[f"expected_{key}"] = str(expected_value)
                    mismatch[f"actual_{key}"] = str(actual_value)
            if mismatch:
                invalid_columns.setdefault(table, {})[column] = mismatch

    missing_unique_constraints: dict[str, list[tuple[str, ...]]] = {}
    for table in present_tables:
        required_unique = _DEV_SCHEMA_UNIQUE_CONSTRAINTS.get(table, ())
        if not required_unique:
            continue
        actual_unique: set[tuple[str, ...]] = set()
        rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
        for row in rows:
            is_unique = int(
                row["unique"] if isinstance(row, sqlite3.Row) else row[2]
            )
            origin = str(
                row["origin"] if isinstance(row, sqlite3.Row) else row[3]
            )
            is_partial = int(
                row["partial"] if isinstance(row, sqlite3.Row) else row[4]
            )
            # Required tuples in this registry are table-declared UNIQUE
            # constraints (or PK autoindexes), never arbitrary CREATE INDEX
            # lookalikes.  Partial or application-created indexes can exclude
            # exactly the values whose idempotency the runtime depends on.
            # Explicit business indexes are independently bound by exact SQL
            # and table ownership below.
            if (
                not is_unique
                or is_partial
                or origin not in {"u", "pk"}
            ):
                continue
            index_name = str(
                row["name"] if isinstance(row, sqlite3.Row) else row[1]
            )
            index_rows = conn.execute(
                f'PRAGMA index_xinfo("{index_name}")'
            ).fetchall()
            index_columns = tuple(
                str(
                    index_row["name"]
                    if isinstance(index_row, sqlite3.Row)
                    else index_row[2]
                )
                for index_row in index_rows
                if int(
                    index_row["key"]
                    if isinstance(index_row, sqlite3.Row)
                    else index_row[5]
                )
                and (
                    index_row["name"]
                    if isinstance(index_row, sqlite3.Row)
                    else index_row[2]
                )
                is not None
            )
            actual_unique.add(index_columns)
        missing = [item for item in required_unique if item not in actual_unique]
        if missing:
            missing_unique_constraints[table] = missing

    present_indexes: dict[str, dict[str, str]] = {}
    if indexes:
        placeholders = ", ".join("?" for _ in indexes)
        rows = conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            f"WHERE type='index' AND name IN ({placeholders})",
            indexes,
        ).fetchall()
        present_indexes = {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[0]): {
                "table": str(
                    row["tbl_name"] if isinstance(row, sqlite3.Row) else row[1]
                ),
                "sql": str(
                    (row["sql"] if isinstance(row, sqlite3.Row) else row[2]) or ""
                ),
            }
            for row in rows
        }
    missing_indexes = sorted(set(indexes) - set(present_indexes))

    def normalize_sql(value: str) -> str:
        normalized = re.sub(
            r"\s+", " ", str(value or "").strip().rstrip(";")
        ).lower()
        return re.sub(r"\s*([(),])\s*", r"\1", normalized)

    invalid_indexes: dict[str, dict[str, str]] = {}
    for index, expected in index_definitions.items():
        actual = present_indexes.get(index)
        if actual is None:
            continue
        expected_sql = normalize_sql(expected["sql"])
        actual_sql = normalize_sql(actual["sql"])
        if actual["table"] != expected["table"] or actual_sql != expected_sql:
            invalid_indexes[index] = {
                "expected_table": expected["table"],
                "actual_table": actual["table"],
                "expected_sql": expected_sql,
                "actual_sql": actual_sql,
            }
    if (
        missing_tables
        or missing_columns
        or missing_indexes
        or invalid_indexes
        or invalid_columns
        or missing_unique_constraints
    ):
        raise DevRuntimeSchemaVerificationError(
            owner,
            missing_tables=missing_tables,
            missing_columns=missing_columns,
            missing_indexes=missing_indexes,
            invalid_indexes=invalid_indexes,
            invalid_columns=invalid_columns,
            missing_unique_constraints=missing_unique_constraints,
        )


def sqlite_write_lock() -> threading.RLock:
    """Process-local write serialization for governance SQLite mutations.

    SQLite WAL permits concurrent readers but still has a single writer.  The
    governance HTTP server is threaded, so short task/queue writes can collide
    inside one process before SQLite's busy timeout has a chance to smooth the
    flow.  Callers should hold this lock only around direct DB mutation +
    commit blocks, never around model calls or slow external work.
    """
    return _SQLITE_WRITE_LOCK


def canonical_ac_database_identity(
    conn: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Return the public-safe physical identity of this runtime world's AC DB.

    The identity deliberately excludes the host's absolute path.  Normal
    SQLite writes preserve device/inode, while file substitution, an alternate
    shared volume, or a symlink escape changes or invalidates the identity.
    Stable-plane callers are additionally pinned to
    ``$AMING_CLAW_HOME/shared-volume``; dev-plane callers may open that same
    file but cannot nominate a different AC-shaped database.
    """

    if _is_dev_runtime():
        db_path = _dev_database_path()
        metadata = db_path.stat(follow_symlinks=False)
        if db_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("canonical AC dev database must be a non-symlink file")
        if db_path.resolve(strict=True) != db_path.absolute():
            raise ValueError("canonical AC dev database escaped its world root")
        if conn is not None:
            rows = conn.execute("PRAGMA database_list").fetchall()
            main_paths = [
                Path(str(row[2])).resolve(strict=True)
                for row in rows
                if str(row[1]) == "main" and str(row[2])
            ]
            if main_paths != [db_path.resolve(strict=True)]:
                raise ValueError("opened AC dev database identity mismatch")
        with closing(sqlite3.connect(db_path)) as identity_conn:
            meta = dict(identity_conn.execute("SELECT key, value FROM schema_meta"))
        genesis_sha256 = str(meta.get("governance_world_genesis_sha256") or "")
        if (
            meta.get("governance_world_id") != AC_DEV_WORLD_ID
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", genesis_sha256)
        ):
            raise ValueError("canonical AC dev database genesis is invalid")
        return {
            "schema_version": "ac_governance_database_identity.v2",
            "world_id": AC_DEV_WORLD_ID,
            "project_id": AC_PROJECT_ID,
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "relative_path_sha256": "sha256:"
            + hashlib.sha256(AC_DATABASE_DEV_RELATIVE_PATH.encode("utf-8")).hexdigest(),
            "genesis_sha256": genesis_sha256,
        }

    shared_raw = os.environ.get("SHARED_VOLUME_PATH", "").strip()
    if not shared_raw:
        raise RuntimeError("canonical AC database requires SHARED_VOLUME_PATH")
    shared_input = Path(shared_raw).expanduser().absolute()
    if shared_input.is_symlink():
        raise ValueError("canonical AC shared volume cannot be a symlink")
    shared_root = shared_input.resolve(strict=True)
    if shared_root != shared_input or not shared_root.is_dir():
        raise ValueError("canonical AC shared volume identity mismatch")
    if os.environ.get(RUNTIME_PLANE_ENV, "").strip().lower() == "stable":
        stable_raw = os.environ.get("AMING_CLAW_HOME", "").strip()
        if not stable_raw:
            raise RuntimeError("stable AC database requires AMING_CLAW_HOME")
        stable_root = Path(stable_raw).expanduser().resolve(strict=True)
        expected_shared = (stable_root / "shared-volume").absolute()
        if (
            expected_shared.is_symlink()
            or expected_shared.resolve(strict=True) != expected_shared
            or shared_root != expected_shared
        ):
            raise ValueError(
                "stable AC database must use the stable worktree shared volume"
            )
    relative_db = Path(AC_DATABASE_STABLE_RELATIVE_PATH).relative_to(
        "shared-volume"
    )
    db_path = (shared_root / relative_db).absolute()
    if db_path.is_symlink():
        raise ValueError("canonical AC database cannot be a symlink")
    resolved_db = db_path.resolve(strict=True)
    if resolved_db != db_path:
        raise ValueError("canonical AC database escaped its stable path")
    metadata = db_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("canonical AC database must be a regular file")
    if conn is not None:
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_paths = [
            Path(str(row[2])).resolve(strict=True)
            for row in rows
            if str(row[1]) == "main" and str(row[2])
        ]
        if main_paths != [resolved_db]:
            raise ValueError("opened AC database identity mismatch")
    relative_hash = "sha256:" + hashlib.sha256(
        AC_DATABASE_STABLE_RELATIVE_PATH.encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "ac_stable_database_identity.v1",
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "stable_relative_path_sha256": relative_hash,
    }

SCHEMA_SQL = """
-- Node runtime state
CREATE TABLE IF NOT EXISTS node_state (
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    verify_status TEXT NOT NULL DEFAULT 'pending',
    build_status  TEXT NOT NULL DEFAULT 'impl:missing',
    evidence_json TEXT,
    updated_by    TEXT,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, node_id)
);

-- Node state history (event sourcing auxiliary)
CREATE TABLE IF NOT EXISTS node_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    role          TEXT NOT NULL,
    evidence_json TEXT,
    session_id    TEXT,
    ts            TEXT NOT NULL,
    version       INTEGER NOT NULL
);

-- Session management
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    principal_id  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    scope_json    TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_heartbeat TEXT,
    metadata_json TEXT
);

-- Task registry (v4: upgraded from file-based)
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'created',
    type          TEXT NOT NULL DEFAULT 'task',
    prompt        TEXT,
    related_nodes TEXT,
    assigned_to   TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    result_json   TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    priority      INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    retry_round   INTEGER NOT NULL DEFAULT 0,
    parent_task_id TEXT
);
-- idx_tasks_status and idx_tasks_assigned created in migration v2

-- Task attempts (retry tracking)
CREATE TABLE IF NOT EXISTS task_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_num   INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    result_json   TEXT,
    error_message TEXT
);

-- Append-only task implementation timeline. Backlog rows describe the work;
-- timeline rows describe what agents/executors/gates actually did.
CREATE TABLE IF NOT EXISTS task_timeline_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           TEXT NOT NULL,
    backlog_id           TEXT NOT NULL DEFAULT '',
    mf_id                TEXT NOT NULL DEFAULT '',
    task_id              TEXT NOT NULL DEFAULT '',
    attempt_num          INTEGER NOT NULL DEFAULT 0,
    event_type           TEXT NOT NULL,
    phase                TEXT NOT NULL DEFAULT '',
    event_kind           TEXT NOT NULL DEFAULT '',
    scenario_id          TEXT NOT NULL DEFAULT '',
    parent_event_id      INTEGER NOT NULL DEFAULT 0,
    correlation_id       TEXT NOT NULL DEFAULT '',
    severity             TEXT NOT NULL DEFAULT '',
    decision             TEXT NOT NULL DEFAULT '',
    schema_version       INTEGER NOT NULL DEFAULT 2,
    actor                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT '',
    payload_json         TEXT NOT NULL DEFAULT '{}',
    verification_json    TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
    trace_id             TEXT NOT NULL DEFAULT '',
    commit_sha           TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_timeline_task
    ON task_timeline_events(project_id, task_id, attempt_num, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_backlog
    ON task_timeline_events(project_id, backlog_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_trace
    ON task_timeline_events(project_id, trace_id, id);

-- Idempotency keys
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

-- Audit index (raw events in JSONL, this is the query index)
CREATE TABLE IF NOT EXISTS audit_index (
    event_id      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    event         TEXT NOT NULL,
    actor         TEXT,
    ok            INTEGER NOT NULL DEFAULT 1,
    ts            TEXT NOT NULL,
    node_ids      TEXT
);

-- Version snapshots (for rollback)
CREATE TABLE IF NOT EXISTS snapshots (
    project_id    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    PRIMARY KEY (project_id, version)
);

-- Event outbox (transactional outbox pattern)
CREATE TABLE IF NOT EXISTS event_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    dead_letter   INTEGER NOT NULL DEFAULT 0,
    trace_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON event_outbox(delivered_at) WHERE delivered_at IS NULL AND dead_letter = 0;
CREATE INDEX IF NOT EXISTS idx_outbox_dead ON event_outbox(dead_letter) WHERE dead_letter = 1;

-- Per-project chain version (auto-chain integrity seal)
CREATE TABLE IF NOT EXISTS project_version (
    project_id    TEXT PRIMARY KEY,
    chain_version TEXT NOT NULL,     -- git short hash from last auto-merge
    updated_at    TEXT NOT NULL,     -- ISO 8601
    updated_by    TEXT NOT NULL,     -- "auto-chain" | "init" | "register"
    git_head      TEXT DEFAULT '',   -- current git HEAD (synced by executor)
    dirty_files   TEXT DEFAULT '[]', -- JSON array of uncommitted files
    git_synced_at TEXT DEFAULT ''    -- when executor last synced git status
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_session_principal ON sessions(principal_id, project_id);
CREATE INDEX IF NOT EXISTS idx_session_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_session_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_audit_project_ts ON audit_index(project_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_ok ON audit_index(ok);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_keys(expires_at);
CREATE INDEX IF NOT EXISTS idx_node_history_project ON node_history(project_id, node_id, ts);

-- Chain context events (event-sourced, append-only)
CREATE TABLE IF NOT EXISTS chain_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_task_id  TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_events_root ON chain_events(root_task_id, ts);
CREATE INDEX IF NOT EXISTS idx_chain_events_task ON chain_events(task_id, event_type, ts);

-- Durable intake for structured AI output envelopes.
CREATE TABLE IF NOT EXISTS ai_outputs (
    output_id                    TEXT PRIMARY KEY,
    project_id                   TEXT NOT NULL,
    snapshot_id                  TEXT NOT NULL DEFAULT '',
    base_commit                  TEXT NOT NULL DEFAULT '',
    task_type                    TEXT NOT NULL,
    target_type                  TEXT NOT NULL DEFAULT '',
    target_id                    TEXT NOT NULL DEFAULT '',
    producer                     TEXT NOT NULL DEFAULT '',
    source_run_id                TEXT NOT NULL DEFAULT '',
    provider                     TEXT NOT NULL DEFAULT '',
    model                        TEXT NOT NULL DEFAULT '',
    prompt_hash                  TEXT NOT NULL DEFAULT '',
    payload_hash                 TEXT NOT NULL DEFAULT '',
    dedupe_key                   TEXT NOT NULL,
    idempotency_key              TEXT NOT NULL DEFAULT '',
    status                       TEXT NOT NULL DEFAULT 'submitted',
    route_status                 TEXT NOT NULL DEFAULT 'queued',
    payload_json                 TEXT NOT NULL DEFAULT '{}',
    self_precheck_json           TEXT NOT NULL DEFAULT '{}',
    graph_query_trace_ids_json   TEXT NOT NULL DEFAULT '[]',
    metadata_json                TEXT NOT NULL DEFAULT '{}',
    created_by                   TEXT NOT NULL DEFAULT '',
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,
    UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_project_created
    ON ai_outputs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_project_type_status
    ON ai_outputs(project_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_target
    ON ai_outputs(project_id, target_type, target_id);

CREATE TABLE IF NOT EXISTS ai_output_events (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id                    TEXT NOT NULL,
    project_id                   TEXT NOT NULL,
    event_type                   TEXT NOT NULL,
    actor                        TEXT NOT NULL DEFAULT '',
    request_id                   TEXT NOT NULL DEFAULT '',
    payload_json                 TEXT NOT NULL DEFAULT '{}',
    created_at                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_output_events_output
    ON ai_output_events(output_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_output_events_project
    ON ai_output_events(project_id, created_at);

CREATE TABLE IF NOT EXISTS ai_output_queue (
    output_id                    TEXT PRIMARY KEY,
    project_id                   TEXT NOT NULL,
    task_type                    TEXT NOT NULL,
    target_type                  TEXT NOT NULL DEFAULT '',
    target_id                    TEXT NOT NULL DEFAULT '',
    status                       TEXT NOT NULL DEFAULT 'queued',
    priority                     INTEGER NOT NULL DEFAULT 0,
    attempt_count                INTEGER NOT NULL DEFAULT 0,
    max_attempts                 INTEGER NOT NULL DEFAULT 3,
    lease_token                  TEXT NOT NULL DEFAULT '',
    claimed_by                   TEXT NOT NULL DEFAULT '',
    claimed_at                   TEXT NOT NULL DEFAULT '',
    lease_expires_at             TEXT NOT NULL DEFAULT '',
    last_error                   TEXT NOT NULL DEFAULT '',
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_output_queue_project_status
    ON ai_output_queue(project_id, status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_output_queue_project_type
    ON ai_output_queue(project_id, task_type, status);

-- Gate events audit trail (queryable gate history per task)
CREATE TABLE IF NOT EXISTS gate_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    gate_name     TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    reason        TEXT,
    trace_id      TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_events_project_task ON gate_events(project_id, task_id);

-- Pending nodes: inferred doc associations awaiting human review (P4)
CREATE TABLE IF NOT EXISTS pending_nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    doc_path      TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.0,
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    reviewed_at   TEXT,
    reviewed_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_nodes_project ON pending_nodes(project_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_nodes_node ON pending_nodes(project_id, node_id);

-- Backlog bugs (DB-first backlog storage, OPT-DB-BACKLOG)
CREATE TABLE IF NOT EXISTS backlog_bugs (
    bug_id              TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'OPEN',
    priority            TEXT NOT NULL DEFAULT 'P3',
    target_files        TEXT NOT NULL DEFAULT '[]',
    test_files          TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    chain_task_id       TEXT NOT NULL DEFAULT '',
    "commit"            TEXT NOT NULL DEFAULT '',
    discovered_at       TEXT NOT NULL DEFAULT '',
    fixed_at            TEXT NOT NULL DEFAULT '',
    details_md          TEXT NOT NULL DEFAULT '',
    chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
    required_docs       TEXT NOT NULL DEFAULT '[]',
    provenance_paths    TEXT NOT NULL DEFAULT '[]',
    chain_stage         TEXT NOT NULL DEFAULT '',
    last_failure_reason TEXT NOT NULL DEFAULT '',
    stage_updated_at    TEXT NOT NULL DEFAULT '',
    runtime_state       TEXT NOT NULL DEFAULT '',
    current_task_id     TEXT NOT NULL DEFAULT '',
    root_task_id        TEXT NOT NULL DEFAULT '',
    worktree_path       TEXT NOT NULL DEFAULT '',
    worktree_branch     TEXT NOT NULL DEFAULT '',
    bypass_policy_json  TEXT NOT NULL DEFAULT '{}',
    mf_type             TEXT NOT NULL DEFAULT '',
    takeover_json       TEXT NOT NULL DEFAULT '{}',
    runtime_updated_at  TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backlog_bugs_status ON backlog_bugs(status);
CREATE INDEX IF NOT EXISTS idx_backlog_bugs_priority ON backlog_bugs(priority);

-- Reconcile sessions (CR0a: one-active-per-project invariant + state machine)
CREATE TABLE IF NOT EXISTS reconcile_sessions (
    project_id              TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    run_id                  TEXT,
    status                  TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','finalizing','finalize_failed','finalized','rolled_back')),
    started_at              TEXT NOT NULL,
    finalized_at            TEXT,
    cluster_count_total     INTEGER NOT NULL DEFAULT 0,
    cluster_count_resolved  INTEGER NOT NULL DEFAULT 0,
    cluster_count_failed    INTEGER NOT NULL DEFAULT 0,
    bypass_gates_json       TEXT NOT NULL DEFAULT '[]',
    started_by              TEXT,
    snapshot_path           TEXT,
    snapshot_head_sha       TEXT,
    base_commit_sha         TEXT NOT NULL DEFAULT '',
    target_branch           TEXT NOT NULL DEFAULT '',
    target_head_sha         TEXT NOT NULL DEFAULT '',
    finalize_error_json     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, session_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_sessions_one_active
    ON reconcile_sessions (project_id)
    WHERE status IN ('active','finalizing','finalize_failed');

-- Reconcile batch memory (PM semantic merge context for cluster batches)
CREATE TABLE IF NOT EXISTS reconcile_batch_memory (
    project_id       TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active',
    memory_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_session
    ON reconcile_batch_memory (project_id, session_id);
CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_status
    ON reconcile_batch_memory (project_id, status);

-- Reconcile file inventory (full-project coverage ledger before graph rebase finalize)
CREATE TABLE IF NOT EXISTS reconcile_file_inventory (
    project_id        TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    path              TEXT NOT NULL,
    file_kind         TEXT NOT NULL DEFAULT '',
    language          TEXT NOT NULL DEFAULT '',
    sha256            TEXT NOT NULL DEFAULT '',
    file_hash         TEXT NOT NULL DEFAULT '',
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    last_scanned_commit TEXT NOT NULL DEFAULT '',
    graph_status      TEXT NOT NULL DEFAULT '',
    mapped_node_ids   TEXT NOT NULL DEFAULT '[]',
    attached_node_ids TEXT NOT NULL DEFAULT '[]',
    attachment_role   TEXT NOT NULL DEFAULT '',
    attachment_source TEXT NOT NULL DEFAULT '',
    scan_status       TEXT NOT NULL DEFAULT '',
    cluster_id        TEXT NOT NULL DEFAULT '',
    candidate_node_id TEXT NOT NULL DEFAULT '',
    attached_to       TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    decision          TEXT NOT NULL DEFAULT 'pending',
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, run_id, path)
);
CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_status
    ON reconcile_file_inventory (project_id, run_id, scan_status);
CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_kind
    ON reconcile_file_inventory (project_id, run_id, file_kind);

-- Commit-bound graph asset projection. JSON artifacts remain replay/debug
-- exports; this table is the runtime projection for doc/test/config assets.
CREATE TABLE IF NOT EXISTS graph_asset_projection (
    project_id              TEXT NOT NULL,
    snapshot_id             TEXT NOT NULL DEFAULT '',
    run_id                  TEXT NOT NULL DEFAULT '',
    commit_sha              TEXT NOT NULL DEFAULT '',
    asset_kind              TEXT NOT NULL DEFAULT '',
    asset_path              TEXT NOT NULL DEFAULT '',
    file_kind               TEXT NOT NULL DEFAULT '',
    sha256                  TEXT NOT NULL DEFAULT '',
    file_hash               TEXT NOT NULL DEFAULT '',
    size_bytes              INTEGER NOT NULL DEFAULT 0,
    scan_status             TEXT NOT NULL DEFAULT '',
    graph_status            TEXT NOT NULL DEFAULT '',
    binding_status          TEXT NOT NULL DEFAULT '',
    impact_scope_policy     TEXT NOT NULL DEFAULT '',
    accepted_bindings_json  TEXT NOT NULL DEFAULT '[]',
    binding_candidates_json TEXT NOT NULL DEFAULT '[]',
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    source_projection       TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (project_id, snapshot_id, commit_sha, asset_kind, asset_path)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_projection_snapshot
    ON graph_asset_projection (project_id, snapshot_id, asset_kind, binding_status);
CREATE INDEX IF NOT EXISTS idx_graph_asset_projection_path
    ON graph_asset_projection (project_id, asset_kind, asset_path);

CREATE TABLE IF NOT EXISTS graph_asset_bindings (
    project_id          TEXT NOT NULL,
    snapshot_id         TEXT NOT NULL DEFAULT '',
    commit_sha          TEXT NOT NULL DEFAULT '',
    asset_kind          TEXT NOT NULL DEFAULT '',
    asset_path          TEXT NOT NULL DEFAULT '',
    binding_status      TEXT NOT NULL DEFAULT '',
    node_id             TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT '',
    binding_key         TEXT NOT NULL DEFAULT '',
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, snapshot_id, commit_sha, asset_kind, asset_path, binding_status, node_id, binding_key)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_bindings_node
    ON graph_asset_bindings (project_id, snapshot_id, node_id, asset_kind, binding_status);
CREATE INDEX IF NOT EXISTS idx_graph_asset_bindings_path
    ON graph_asset_bindings (project_id, asset_kind, asset_path);
"""


def _absolute_non_symlink_root(path: Path, *, create: bool) -> Path:
    """Resolve one physical root without accepting a symlink at any component."""

    absolute = path.expanduser().absolute()
    probe = absolute
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_symlink():
        raise ValueError("AC dev storage root cannot contain a symlink")
    if probe.resolve(strict=True) != probe:
        raise ValueError("AC dev storage root escaped its physical parent")
    if absolute.exists():
        if absolute.is_symlink():
            raise ValueError("AC dev storage root cannot be a symlink")
        if absolute.resolve(strict=True) != absolute or not absolute.is_dir():
            raise ValueError("AC dev storage root identity mismatch")
    elif create:
        absolute.mkdir(parents=True, exist_ok=False)
    else:
        raise FileNotFoundError("AC dev storage root does not exist: " + str(absolute))
    if absolute.is_symlink() or absolute.resolve(strict=True) != absolute:
        raise ValueError("AC dev storage root cannot be a symlink")
    return absolute


def _validated_isolated_dev_receipt(
    receipt_path: Path, root: Path, source_identity: Mapping[str, object],
    stable_binding: Mapping[str, object], *, allow_postimage: bool = False,
) -> Path:
    archive = root / "archive" / "schema-admission"
    path = receipt_path.expanduser().absolute()
    details = path.lstat()
    match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
    if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or path.is_symlink()
            or path.resolve(strict=True) != path or path.parent.resolve(strict=True) != archive.resolve(strict=True)
            or match is None):
        raise ValueError("AC dev isolated receipt is not a canonical regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != match.group(1):
        raise ValueError("AC dev isolated receipt raw digest mismatch")
    sidecar = archive / f"{match.group(1)}.sha256"
    side = sidecar.lstat()
    if (not stat.S_ISREG(side.st_mode) or side.st_nlink != 1 or sidecar.is_symlink()
            or sidecar.read_text(encoding="utf-8") != f"sha256:{match.group(1)}  {path.name}\n"):
        raise ValueError("AC dev isolated receipt sidecar mismatch")
    receipt = json.loads(raw)
    database = root / AC_DATABASE_DEV_RELATIVE_PATH
    root_stat = root.stat(follow_symlinks=False)
    db_stat = database.stat(follow_symlinks=False)
    inventory = receipt.get("schema_inventory_after") if isinstance(receipt, Mapping) else None
    plan_sha = "sha256:" + hashlib.sha256(
        json.dumps(authority_projection_schema_plan(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    digest = hashlib.sha256()
    descriptor = os.open(database, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    expected_source = dict(source_identity)
    source = receipt.get("source_identity") if isinstance(receipt, Mapping) else None
    cli_source = source.get("cli_source") if isinstance(source, Mapping) else None
    receipt_database_sha = str(receipt.get("database_sha256_after") or "")
    current_database_sha = "sha256:" + digest.hexdigest()
    if (
        receipt.get("schema_version") != "ac_dev_offline_schema_admission.v3"
        or receipt.get("stage") != "completed" or receipt.get("changed") is not False
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("previous_receipt_sha256") or ""))
        or receipt.get("project_id") != AC_PROJECT_ID or receipt.get("port") != 40008
        or receipt.get("root_identity") != {"path": str(root), "device": int(root_stat.st_dev), "inode": int(root_stat.st_ino)}
        or receipt.get("database_identity") != {"path": str(database), "device": int(db_stat.st_dev), "inode": int(db_stat.st_ino)}
        or (receipt_database_sha != current_database_sha and not allow_postimage)
        or cli_source != expected_source or receipt.get("plan_sha256") != plan_sha
        or not isinstance(inventory, Mapping)
        or inventory.get("sha256") != AC_AUTHORITY_SCHEMA_INVENTORY_SHA256
        or not isinstance(inventory.get("inventory"), list)
        or len(inventory["inventory"]) != AC_AUTHORITY_SCHEMA_INVENTORY_COUNT
        or "sha256:" + hashlib.sha256(json.dumps(
            inventory["inventory"], separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest() != AC_AUTHORITY_SCHEMA_INVENTORY_SHA256
    ):
        raise ValueError("AC dev isolated receipt binding mismatch")
    if receipt_database_sha != current_database_sha:
        # A linked-v3 receipt is immutable authority for the admitted preimage.
        # After the child-owned custody transaction the current database is a
        # postimage, so validate its source-owned genesis without pretending
        # that the historical receipt names the new bytes.
        uri = "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            meta = dict(connection.execute(
                "SELECT key, value FROM schema_meta WHERE key IN "
                "('governance_world_id','governance_world_genesis_json',"
                "'governance_world_genesis_sha256')"
            ))
            genesis = json.loads(str(meta.get("governance_world_genesis_json") or ""))
            if (meta.get("governance_world_id") != AC_DEV_WORLD_ID
                    or not isinstance(genesis, Mapping)
                    or genesis.get("world_id") != AC_DEV_WORLD_ID
                    or genesis.get("project_id") != AC_PROJECT_ID
                    or meta.get("governance_world_genesis_sha256") != _world_genesis_hash(genesis)):
                raise ValueError("AC dev isolated postimage genesis mismatch")
        finally:
            connection.close()
    stable_database = Path(str(stable_binding["database_path"])).resolve(strict=True)
    stable_stat = stable_database.stat(follow_symlinks=False)
    stable_root = Path(str(stable_binding["shared_volume_path"])).resolve(strict=True)
    from agent.runtime_plane import resolve_ac_dev_storage_root
    canonical_dev_root = resolve_ac_dev_storage_root(stable_root)
    source_root = Path(str(source_identity.get("root") or "")).resolve(strict=True)
    if ((db_stat.st_dev, db_stat.st_ino) == (stable_stat.st_dev, stable_stat.st_ino)
            or root in {stable_root, canonical_dev_root, source_root}
            or root in stable_root.parents or stable_root in root.parents
            or root in source_root.parents or source_root in root.parents):
        raise ValueError("AC dev isolated root overlaps stable custody")
    return root


def _validated_canonical_legacy_postimage_adoption(
    root: Path, receipt_path: Path, source_identity: Mapping[str, object],
    stable_binding: Mapping[str, object],
) -> Path:
    """Accept only the immutable audited bridge from the canonical legacy world."""
    directory = root / "archive" / "canonical-legacy-postimage-adoption"
    receipts = sorted(directory.glob("adoption.*.json")) if directory.is_dir() else []
    if len(receipts) != 1:
        raise ValueError("AC dev canonical adoption receipt is missing or ambiguous")
    path = receipts[0]
    match = re.fullmatch(r"adoption\.([0-9a-f]{64})\.json", path.name)
    raw = path.read_bytes()
    try:
        adoption = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("AC dev canonical adoption receipt is unreadable") from exc
    database = root / AC_DATABASE_DEV_RELATIVE_PATH
    root_stat = root.stat(follow_symlinks=False)
    db_stat = database.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    descriptor = os.open(database, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    current_sha256 = "sha256:" + digest.hexdigest()
    linked = receipt_path.expanduser().absolute()
    linked_raw = linked.read_bytes()
    linked_digest = "sha256:" + hashlib.sha256(linked_raw).hexdigest()
    candidate = dict(adoption.get("candidate_source_identity") or {}) if isinstance(adoption, Mapping) else {}
    stable_database = Path(str(stable_binding["database_path"])).resolve(strict=True)
    stable_stat = stable_database.stat(follow_symlinks=False)
    if (
        path.is_symlink() or not path.is_file() or match is None
        or hashlib.sha256(raw).hexdigest() != match.group(1)
        or adoption.get("schema_version") != "ac_dev_canonical_legacy_postimage_adoption.v1"
        or adoption.get("stage") != "completed"
        or adoption.get("project_id") != AC_PROJECT_ID or adoption.get("port") != 40008
        or adoption.get("root_identity") != {
            "path": str(root), "device": int(root_stat.st_dev), "inode": int(root_stat.st_ino),
        }
        or adoption.get("database_identity") != {
            "path": str(database), "device": int(db_stat.st_dev), "inode": int(db_stat.st_ino),
        }
        or adoption.get("linked_v3_receipt") != str(linked)
        or adoption.get("linked_v3_receipt_sha256") != linked_digest
        or candidate != dict(source_identity)
        or (db_stat.st_dev, db_stat.st_ino) == (stable_stat.st_dev, stable_stat.st_ino)
    ):
        raise ValueError("AC dev canonical adoption receipt mismatch")
    historical = dict(adoption.get("receipt_source_identity") or {})
    try:
        linked_value = json.loads(linked_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("AC dev canonical adoption linked receipt is unreadable") from exc
    linked_source = linked_value.get("source_identity") if isinstance(linked_value, Mapping) else None
    if (not isinstance(linked_source, Mapping)
            or dict(linked_source.get("cli_source") or {}) != historical
            or linked_value.get("database_sha256_after") != adoption.get("database_sha256_preimage")):
        raise ValueError("AC dev canonical adoption predecessor mismatch")
    for suffix in ("-wal", "-shm", "-journal"):
        companion = Path(str(database) + suffix)
        if companion.exists() or companion.is_symlink():
            raise ValueError("AC dev canonical adoption sidecar drift")
    if adoption.get("database_sha256_postimage") != current_sha256:
        # Once the adoption receipt has authorized the legacy postimage, the
        # ordinary child custody transaction may advance only the same four
        # schema_meta custody fields.  Re-prove every other logical byte.
        if (not isinstance(adoption.get("non_schema_meta_projection"), Mapping)
                or not isinstance(adoption.get("schema_meta_postimage"), Mapping)):
            raise ValueError("AC dev canonical adoption post-custody authority is incomplete")
        uri = "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("AC dev canonical adoption post-custody quick-check failed")
            projection = _sqlite_logical_projection(
                connection, exclude_tables=frozenset({"schema_meta"}),
            )
            meta = {str(key): str(value) for key, value in connection.execute(
                "SELECT key,value FROM schema_meta ORDER BY key"
            )}
        finally:
            connection.close()
        if projection != adoption.get("non_schema_meta_projection"):
            raise ValueError("AC dev canonical adoption post-custody projection mismatch")
        baseline_meta = dict(adoption.get("schema_meta_postimage") or {})
        custody_fields = {
            "governance_world_current_process_json", "governance_world_source_tip_json",
            "governance_world_source_tip_revision", "governance_world_source_tip_sha256",
        }
        if ({key: value for key, value in meta.items() if key not in custody_fields}
                != {key: value for key, value in baseline_meta.items() if key not in custody_fields}):
            raise ValueError("AC dev canonical adoption post-custody metadata mismatch")
        try:
            tip = json.loads(meta["governance_world_source_tip_json"])
            process = json.loads(meta["governance_world_current_process_json"])
            revision = int(meta["governance_world_source_tip_revision"])
            baseline_revision = int(baseline_meta["governance_world_source_tip_revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("AC dev canonical adoption post-custody metadata is invalid") from exc
        expected_tip = {key: source_identity.get(key) for key in (
            "root", "branch", "commit", "source_sha256",
        )}
        if (tip != expected_tip or meta.get("governance_world_source_tip_sha256")
                != _world_source_tip_hash(expected_tip)
                or revision < baseline_revision + 1
                or not isinstance(process, Mapping)
                or process.get("source_root") != source_identity.get("root")
                or process.get("source_commit") != source_identity.get("commit")
                or process.get("project_id") != AC_PROJECT_ID or process.get("port") != 40008):
            raise ValueError("AC dev canonical adoption post-custody binding mismatch")
    return root


def _dev_storage_root(*, create: bool = False, isolated_receipt: Path | None = None,
                      source_identity: Mapping[str, object] | None = None,
                      allow_postimage: bool = False) -> Path:
    """Resolve the only AC dev world; raw env values are assertions, not authority."""
    # Reject the missing required dev claim before contacting any authority or
    # resolving a potentially hostile sibling path.  This is zero-mutation.
    raw = os.environ.get(AC_DEV_STORAGE_ROOT_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            "AC dev runtime requires an explicit AMING_CLAW_DEV_STORAGE_ROOT"
        )
    binding = verified_stable_database_binding()
    _revalidate_stable_database_binding(binding)
    stable = _absolute_non_symlink_root(Path(str(binding["shared_volume_path"])), create=False)
    stable_raw = os.environ.get(AC_STABLE_SHARED_VOLUME_ENV, "").strip()
    if not stable_raw or _absolute_non_symlink_root(Path(stable_raw), create=False) != stable:
        raise RuntimeError("AC dev stable shared-volume claim mismatches verified authority")
    supplied = Path(raw).expanduser().absolute()
    if isolated_receipt is not None:
        root = _absolute_non_symlink_root(supplied, create=False)
        from agent.runtime_plane import resolve_ac_dev_storage_root
        try:
            canonical_root = resolve_ac_dev_storage_root(stable)
        except (OSError, RuntimeError, ValueError):
            canonical_root = None
        if canonical_root is not None and root == canonical_root:
            return _validated_canonical_legacy_postimage_adoption(
                root, isolated_receipt, source_identity or {}, binding,
            )
        return _validated_isolated_dev_receipt(
            isolated_receipt, root, source_identity or {}, binding,
            allow_postimage=allow_postimage,
        )
    from agent.runtime_plane import resolve_ac_dev_storage_root
    expected = resolve_ac_dev_storage_root(stable)
    # Compare before any mkdir/open; a symlink/traversal is an invalid claim.
    if supplied.is_symlink():
        raise ValueError("AC dev storage root cannot be a symlink")
    if supplied != expected:
        root = _absolute_non_symlink_root(supplied, create=False)
        runtime = root / "runtime" / "durable-launch"
        launches = []
        for candidate in runtime.glob("launch.*.json"):
            try:
                value = json.loads(candidate.read_bytes())
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(value, Mapping) and value.get("pid") == os.getpid():
                launches.append(candidate)
        if len(launches) != 1:
            raise ValueError("AC dev storage root must equal canonical resolver output")
        launch = launches[0]
        match = re.fullmatch(r"launch\.([0-9a-f]{64})\.json", launch.name)
        raw = launch.read_bytes()
        try:
            durable = json.loads(raw)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("AC dev isolated durable root receipt is unreadable") from exc
        database = root / AC_DATABASE_DEV_RELATIVE_PATH
        physical = database.stat(follow_symlinks=False)
        if (launch.is_symlink() or match is None
                or hashlib.sha256(raw).hexdigest() != match.group(1)
                or not isinstance(durable, Mapping)
                or durable.get("schema_version") != "ac_dev_durable_launch.v1"
                or durable.get("stage") != "completed"
                or durable.get("pid") != os.getpid()
                or durable.get("dev_storage_root") != str(root)
                or durable.get("database_path") != str(database)
                or durable.get("project_id") != AC_PROJECT_ID
                or durable.get("port") != 40008
                or dict(durable.get("database_identity") or {}).get("device") != int(physical.st_dev)
                or dict(durable.get("database_identity") or {}).get("inode") != int(physical.st_ino)):
            raise ValueError("AC dev isolated durable root receipt mismatch")
        return root
    return _absolute_non_symlink_root(expected, create=create)


def dev_launch_receipt_path(storage_root: Path | str) -> Path:
    root = _absolute_non_symlink_root(Path(storage_root), create=False)
    return root / AC_DEV_LAUNCH_RECEIPT_NAME


def write_dev_launch_receipt(
    storage_root: Path | str, *, stable_shared_volume: Path | str,
    source_sha256: str, port: int, project_id: str = AC_PROJECT_ID,
) -> dict[str, object]:
    """Persist the source-backed foreground launch admission before server exec."""
    root = _absolute_non_symlink_root(Path(storage_root), create=False)
    stable = _absolute_non_symlink_root(Path(stable_shared_volume), create=False)
    binding = verified_stable_database_binding()
    _revalidate_stable_database_binding(binding)
    current_stable = str(binding.get("shared_volume_path") or "")
    if not current_stable or _absolute_non_symlink_root(Path(current_stable), create=False) != stable:
        raise ValueError("AC dev launch receipt stable volume is not current canonical authority")
    if project_id != AC_PROJECT_ID or port != 40008:
        raise ValueError("AC dev launch receipt requires exact project and port")
    if root == stable or stable in root.parents or root in stable.parents:
        raise ValueError("AC dev launch receipt root must be disjoint from stable volume")
    from agent.runtime_plane import resolve_ac_dev_storage_root
    if root != resolve_ac_dev_storage_root(stable):
        raise ValueError("AC dev launch receipt root must be canonical resolver output")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(source_sha256 or "")):
        raise ValueError("AC dev launch receipt source hash is invalid")
    root_stat = root.stat(follow_symlinks=False)
    stable_stat = stable.stat(follow_symlinks=False)
    parent_stat = stable.parent.stat(follow_symlinks=False)
    receipt = {
        "schema_version": AC_DEV_LAUNCH_RECEIPT_SCHEMA,
        "world_id": AC_DEV_WORLD_ID, "project_id": AC_PROJECT_ID,
        "runtime_plane": DEV_RUNTIME_PLANE, "port": 40008, "background": False,
        "storage_root": str(root), "storage_device": int(root_stat.st_dev), "storage_inode": int(root_stat.st_ino),
        "stable_shared_volume": str(stable),
        "stable_shared_volume_device": int(stable_stat.st_dev),
        "stable_shared_volume_inode": int(stable_stat.st_ino),
        "stable_parent_device": int(parent_stat.st_dev), "stable_parent_inode": int(parent_stat.st_ino),
        "source_sha256": str(source_sha256),
    }
    path = root / AC_DEV_LAUNCH_RECEIPT_NAME
    if path.exists() and path.is_symlink():
        raise ValueError("AC dev launch receipt cannot be a symlink")
    temporary = root / (AC_DEV_LAUNCH_RECEIPT_NAME + ".tmp")
    # The authority is live filesystem identity, not this receipt's path claim.
    stable_after = stable.stat(follow_symlinks=False)
    if (int(stable_after.st_dev), int(stable_after.st_ino)) != (int(stable_stat.st_dev), int(stable_stat.st_ino)):
        raise ValueError("AC dev launch receipt stable volume changed during creation")
    temporary.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)
    return receipt


def validate_dev_launch_receipt(storage_root: Path | str, *, source_sha256: str) -> dict[str, object]:
    """Fail closed before a dev server opens SQLite or takes the writer lease."""
    supplied = Path(storage_root).expanduser().absolute()
    try:
        root = _dev_storage_root(create=False)
        if list((root / "runtime" / "durable-launch").glob("launch.*.json")):
            raise ValueError("isolated durable receipt selected")
    except ValueError:
        # An isolated durable child is released only by the immutable
        # completed receipt written after its unbound custody transaction.
        root = _absolute_non_symlink_root(supplied, create=False)
        runtime = root / "runtime" / "durable-launch"
        candidates = []
        for item in runtime.glob("launch.*.json"):
            try:
                value = json.loads(item.read_bytes())
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(value, Mapping) and value.get("pid") == os.getpid():
                candidates.append(item)
        if len(candidates) != 1:
            raise ValueError("AC dev isolated durable launch receipt is missing")
        candidate = candidates[0]
        match = re.fullmatch(r"launch\.([0-9a-f]{64})\.json", candidate.name)
        raw = candidate.read_bytes()
        try:
            durable = json.loads(raw)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("AC dev isolated durable launch receipt is unreadable") from exc
        database = root / AC_DATABASE_DEV_RELATIVE_PATH
        database_stat = database.stat(follow_symlinks=False)
        if (candidate.is_symlink() or match is None
                or hashlib.sha256(raw).hexdigest() != match.group(1)
                or not isinstance(durable, Mapping)
                or durable.get("schema_version") != "ac_dev_durable_launch.v1"
                or durable.get("stage") != "completed"
                or durable.get("pid") != os.getpid()
                or durable.get("dev_storage_root") != str(root)
                or durable.get("database_path") != str(database)
                or durable.get("project_id") != AC_PROJECT_ID
                or durable.get("port") != 40008
                or durable.get("server_sha256") != source_sha256
                or dict(durable.get("database_identity") or {}).get("device") != int(database_stat.st_dev)
                or dict(durable.get("database_identity") or {}).get("inode") != int(database_stat.st_ino)
                or durable.get("policy") != {"runtime_plane": "dev", "migration": "verify-only",
                    "stable_deployment": "deny", "graph_activation": "deny",
                    "background_workers": "deny"}):
            raise ValueError("AC dev isolated durable launch receipt mismatch")
        return dict(durable)
    if supplied != root:
        raise ValueError("AC dev launch receipt storage root mismatch")
    path = root / AC_DEV_LAUNCH_RECEIPT_NAME
    if not path.is_file() or path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError("AC dev launch receipt is missing or invalid")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("AC dev launch receipt is unreadable") from exc
    root_stat = root.stat(follow_symlinks=False)
    required = {"schema_version": AC_DEV_LAUNCH_RECEIPT_SCHEMA, "world_id": AC_DEV_WORLD_ID,
        "project_id": AC_PROJECT_ID, "runtime_plane": DEV_RUNTIME_PLANE, "port": 40008,
        "background": False, "storage_root": str(root), "storage_device": int(root_stat.st_dev),
        "storage_inode": int(root_stat.st_ino), "source_sha256": source_sha256}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in required.items()):
        raise ValueError("AC dev launch receipt mismatch")
    binding = verified_stable_database_binding()
    _revalidate_stable_database_binding(binding)
    stable = _absolute_non_symlink_root(Path(str(binding.get("shared_volume_path") or "")), create=False)
    if receipt.get("stable_shared_volume") != str(stable):
        raise ValueError("AC dev launch receipt stable volume claim mismatch")
    stable_stat = stable.stat(follow_symlinks=False)
    if (
        receipt.get("stable_shared_volume_device") != int(stable_stat.st_dev)
        or receipt.get("stable_shared_volume_inode") != int(stable_stat.st_ino)
    ):
        raise ValueError("AC dev launch receipt stable volume identity changed")
    parent_stat = stable.parent.stat(follow_symlinks=False)
    if receipt.get("stable_parent_device") != int(parent_stat.st_dev) or receipt.get("stable_parent_inode") != int(parent_stat.st_ino):
        raise ValueError("AC dev launch receipt stable parent identity changed")
    if root == stable or stable in root.parents or root in stable.parents:
        raise ValueError("AC dev launch receipt cross-world root invalid")
    from agent.runtime_plane import resolve_ac_dev_storage_root
    if root != resolve_ac_dev_storage_root(stable):
        raise ValueError("AC dev launch receipt root is not the canonical resolver output")
    return receipt


def _dev_runtime_root(*, create: bool = False) -> Path:
    """Return the dedicated non-symlink root for dev-world runtime artifacts."""

    root = _dev_storage_root(create=create)
    runtime = root / "runtime"
    return _absolute_non_symlink_root(runtime, create=create)


def _dev_database_path() -> Path:
    root = _dev_storage_root(create=False)
    database = (root / AC_DATABASE_DEV_RELATIVE_PATH).absolute()
    if database.is_symlink():
        raise ValueError("AC dev governance database cannot be a symlink")
    if database.parent.resolve(strict=True) != (
        root / "governance" / AC_PROJECT_ID
    ).absolute():
        raise ValueError("AC dev governance database escaped its world root")
    return database


def _world_genesis_hash(genesis: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            dict(genesis),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _world_source_tip_hash(source: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            dict(source),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _verify_dev_source_upgrade(
    previous: Mapping[str, object],
    candidate: Mapping[str, object],
) -> None:
    """Verify one clean exact-branch Git descendant without changing source."""

    try:
        previous_root = Path(
            str(previous.get("root") or "")
        ).expanduser().resolve(strict=True)
        candidate_root = Path(
            str(candidate.get("root") or "")
        ).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("AC dev source upgrade root mismatch") from exc
    if (
        str(previous.get("branch") or "") != "codex/ac-dev"
        or str(candidate.get("branch") or "") != "codex/ac-dev"
    ):
        raise ValueError("AC dev source upgrade branch mismatch")

    def git(root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("AC dev source upgrade Git identity unavailable")
        return result.stdout.strip()

    if Path(git(candidate_root, "rev-parse", "--show-toplevel")).resolve(strict=True) != candidate_root:
        raise ValueError("AC dev source upgrade top-level mismatch")
    if git(candidate_root, "branch", "--show-current") != "codex/ac-dev":
        raise ValueError("AC dev source upgrade checked-out branch mismatch")
    if git(candidate_root, "status", "--porcelain"):
        raise ValueError("AC dev source upgrade requires a clean worktree")
    if git(candidate_root, "rev-parse", "HEAD").lower() != str(candidate.get("commit") or ""):
        raise ValueError("AC dev source upgrade HEAD mismatch")
    if git(candidate_root, "rev-parse", "refs/heads/codex/ac-dev").lower() != str(candidate.get("commit") or ""):
        raise ValueError("AC dev source upgrade canonical ref mismatch")

    if candidate_root != previous_root:
        # The only cross-root continuity accepted here is the exact physical
        # state produced by the governed pointer-only handoff: the old checkout
        # remains byte-for-byte at the stored commit but is detached, while the
        # sole canonical branch checkout is the clean descendant candidate.
        try:
            dev_storage = Path(os.environ[AC_DEV_STORAGE_ROOT_ENV]).expanduser().resolve(strict=True)
            binding = verified_stable_database_binding()
            stable_storage = Path(str(binding.get("shared_volume_path") or "")).expanduser().resolve(strict=True)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise ValueError("AC dev source upgrade protected roots unavailable") from exc
        for source_root in (previous_root, candidate_root):
            if (
                source_root == dev_storage or source_root in dev_storage.parents
                or dev_storage in source_root.parents or source_root == stable_storage
                or source_root in stable_storage.parents or stable_storage in source_root.parents
            ):
                raise ValueError("AC dev source upgrade source/storage roots overlap")
        if Path(git(previous_root, "rev-parse", "--show-toplevel")).resolve(strict=True) != previous_root:
            raise ValueError("AC dev source upgrade previous top-level mismatch")
        previous_common = Path(git(previous_root, "rev-parse", "--git-common-dir"))
        candidate_common = Path(git(candidate_root, "rev-parse", "--git-common-dir"))
        if not previous_common.is_absolute():
            previous_common = previous_root / previous_common
        if not candidate_common.is_absolute():
            candidate_common = candidate_root / candidate_common
        if previous_common.resolve(strict=True) != candidate_common.resolve(strict=True):
            raise ValueError("AC dev source upgrade Git common-dir mismatch")
        worktrees = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=candidate_root,
            capture_output=True, text=True, timeout=10, check=False,
        )
        if worktrees.returncode != 0:
            raise ValueError("AC dev source upgrade worktree registry unavailable")
        registered = {
            Path(line.removeprefix("worktree ")).resolve(strict=True)
            for line in worktrees.stdout.splitlines() if line.startswith("worktree ")
        }
        if previous_root not in registered or candidate_root not in registered:
            raise ValueError("AC dev source upgrade worktree is not registered")
        previous_commit = str(previous.get("commit") or "").lower()
        previous_symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"], cwd=previous_root,
            capture_output=True, text=True, timeout=10, check=False,
        )
        if (
            previous_symbolic.returncode == 0
            or git(previous_root, "rev-parse", "HEAD").lower() != previous_commit
            or git(previous_root, "status", "--porcelain")
            or git(previous_root, "rev-parse", "HEAD^{tree}")
            != git(previous_root, "rev-parse", f"{previous_commit}^{{tree}}")
        ):
            raise ValueError("AC dev source upgrade previous worktree is not exact detached state")
        if str(candidate.get("commit") or "").lower() == previous_commit:
            raise ValueError("AC dev source upgrade pointer-only candidate is not a strict descendant")
    ancestor = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            str(previous.get("commit") or ""),
            str(candidate.get("commit") or ""),
        ],
        cwd=candidate_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("AC dev source upgrade candidate is not a descendant")


def _exclusive_writer_file_lease(database: Path):
    """Acquire the same physical writer fence used by the running dev world."""

    lease_path = Path(str(database) + ".writer.lock")
    handle = open(lease_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        raise RuntimeError("AC dev governance database has a concurrent writer") from exc
    return handle


def _writer_process_start_identity() -> str:
    """Derive the current writer's OS start identity without caller input."""

    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    started = result.stdout if result.returncode == 0 else ""
    if not started.strip():
        raise RuntimeError("AC dev writer process start identity is unavailable")
    return "sha256:" + hashlib.sha256(
        f"{os.getpid()}\0".encode("utf-8") + started.encode("utf-8")
    ).hexdigest()


def acquire_dev_runtime_writer_lease(storage_root: Path | str) -> dict[str, object]:
    """Hold the dedicated dev database's OS writer fence for this process."""

    root = _absolute_non_symlink_root(Path(storage_root), create=False)
    root_metadata = root.stat(follow_symlinks=False)
    database = (root / AC_DATABASE_DEV_RELATIVE_PATH).absolute()
    if database.parent.is_symlink() or database.parent.resolve(strict=True) != database.parent:
        raise ValueError("AC dev governance directory identity mismatch")
    key = str(database)
    owner_start = _writer_process_start_identity()
    with _DEV_DATABASE_WRITER_LEASES_LOCK:
        existing = _DEV_DATABASE_WRITER_LEASES.get(key)
        if existing:
            if (
                int(existing.get("owner_pid") or 0) != os.getpid()
                or existing.get("owner_start_identity") != owner_start
                or existing.get("storage_device") != int(root_metadata.st_dev)
                or existing.get("storage_inode") != int(root_metadata.st_ino)
                or getattr(existing.get("handle"), "closed", True)
            ):
                raise RuntimeError("AC dev governance writer lease owner mismatch")
            return {name: value for name, value in existing.items() if name != "handle"}
        handle = _exclusive_writer_file_lease(database)
        receipt: dict[str, object] = {
            "schema_version": "ac_dev_runtime_writer_lease.v1",
            "world_id": AC_DEV_WORLD_ID,
            "project_id": AC_PROJECT_ID,
            "runtime_plane": DEV_RUNTIME_PLANE,
            "storage_root": str(root),
            "storage_device": int(root_metadata.st_dev),
            "storage_inode": int(root_metadata.st_ino),
            "database_path": key,
            "lease_path": str(Path(str(database) + ".writer.lock")),
            "owner_pid": os.getpid(),
            "owner_start_identity": owner_start,
            "handle": handle,
        }
        _DEV_DATABASE_WRITER_LEASES[key] = receipt
        return {name: value for name, value in receipt.items() if name != "handle"}


def release_dev_runtime_writer_lease(storage_root: Path | str) -> None:
    """Release this process's dedicated dev writer fence during shutdown."""

    root = Path(storage_root).expanduser().absolute()
    key = str((root / AC_DATABASE_DEV_RELATIVE_PATH).absolute())
    with _DEV_DATABASE_WRITER_LEASES_LOCK:
        receipt = _DEV_DATABASE_WRITER_LEASES.pop(key, None)
        if not receipt:
            return
        handle = receipt.get("handle")
        if handle is not None and not getattr(handle, "closed", True):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _bind_dev_writer_lease_database_identity(database: Path) -> None:
    metadata = database.stat(follow_symlinks=False)
    if database.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("AC dev governance database is not a canonical regular file")
    key = str(database.absolute())
    with _DEV_DATABASE_WRITER_LEASES_LOCK:
        receipt = _DEV_DATABASE_WRITER_LEASES.get(key)
        if not receipt:
            raise RuntimeError("AC dev governance writer lease is not held")
        expected = (receipt.get("database_device"), receipt.get("database_inode"))
        actual = (int(metadata.st_dev), int(metadata.st_ino))
        if expected != (None, None) and expected != actual:
            raise ValueError("AC dev governance database changed under writer lease")
        receipt["database_device"], receipt["database_inode"] = actual


_SQLITE_ADOPTION_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _sqlite_adoption_identity(path: Path, *, required: bool) -> dict[str, object] | None:
    """Capture one non-following SQLite artifact identity for adoption CAS.

    This deliberately omits size and mtime: a successful checkpoint is expected
    to change those.  Device/inode/type are the substitution boundary.
    """

    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise ValueError("AC dev SQLite adoption artifact is missing")
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("AC dev SQLite adoption artifact is not a regular file")
    if path.resolve(strict=True) != path.absolute():
        raise ValueError("AC dev SQLite adoption artifact escaped its world")
    return {
        "path": str(path.absolute()),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "mode": int(stat.S_IFMT(metadata.st_mode)),
    }


def _sqlite_adoption_snapshot(root: Path, database: Path) -> dict[str, object]:
    """Read physical identities without following caller-controlled paths."""

    root_meta = root.stat(follow_symlinks=False)
    if root.is_symlink() or not stat.S_ISDIR(root_meta.st_mode):
        raise ValueError("AC dev SQLite adoption root identity is invalid")
    return {
        "root": {"device": int(root_meta.st_dev), "inode": int(root_meta.st_ino)},
        "database": _sqlite_adoption_identity(database, required=True),
        "companions": {
            suffix: _sqlite_adoption_identity(Path(str(database) + suffix), required=False)
            for suffix in ("-wal", "-shm", "-journal")
        },
    }


def _assert_sqlite_adoption_identity(
    before: Mapping[str, object], root: Path, database: Path,
    *, checkpoint_result: tuple[int, int, int],
) -> None:
    """Reject every substitution after a bounded SQLite checkpoint.

    A successful ``TRUNCATE`` is allowed to remove an existing WAL/SHM file on
    close.  It is never allowed to make a different inode look legitimate:
    accepting a replacement would turn a narrow SQLite recovery into an
    attacker-controlled adoption path.
    """

    after = _sqlite_adoption_snapshot(root, database)
    if after["root"] != before.get("root") or after["database"] != before.get("database"):
        raise ValueError("AC dev SQLite adoption identity changed during recovery")
    # A rollback journal is never a resumable dev-world artifact.  WAL/SHM may
    # disappear after a successful TRUNCATE checkpoint, but a new journal is a
    # concurrent/foreign writer signal and must fail closed.
    if after["companions"].get("-journal") is not None:
        raise ValueError("AC dev SQLite adoption found rollback journal")
    if checkpoint_result != (0, 0, 0):
        raise RuntimeError("AC dev SQLite adoption checkpoint did not truncate")
    before_companions = dict(before.get("companions") or {})
    after_companions = dict(after.get("companions") or {})
    for suffix in ("-wal", "-shm"):
        previous = before_companions.get(suffix)
        current = after_companions.get(suffix)
        if previous == current:
            continue
        # SQLite may remove its own sidecar after a completed truncate.  A
        # changed-but-present file is an atomic replacement/recreation and is
        # deliberately not accepted without a stronger directory-fd protocol.
        if previous is not None and current is None:
            continue
        raise ValueError(
            "AC dev SQLite adoption companion identity changed during recovery"
        )


def _assert_no_external_sqlite_holders(database: Path) -> None:
    """Fail closed when another process has one of this world's SQLite files open."""

    paths = {str(Path(str(database) + suffix).absolute()) for suffix in _SQLITE_ADOPTION_SUFFIXES}
    try:
        result = subprocess.run(
            ["lsof", "-n", "-Fpn", *sorted(paths)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("AC dev SQLite holder inspection is unavailable") from exc
    pids = {
        int(line[1:])
        for line in result.stdout.splitlines()
        if line.startswith("p") and line[1:].isdigit()
    }
    foreign = pids - {os.getpid()}
    if foreign:
        raise RuntimeError("AC dev SQLite adoption has external holders")


def _validate_existing_adoption_receipt(root: Path) -> None:
    """Validate a prior canonical receipt without requiring its old source hash.

    The new receipt is written by the CLI only after this recovery and any
    source-tip upgrade succeed.  Requiring the current hash here would make a
    legitimate descendant restart impossible; accepting a copied receipt would
    make it unsafe, so all physical stable/root fields are rechecked.
    """

    path = root / AC_DEV_LAUNCH_RECEIPT_NAME
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
        raise ValueError("existing AC dev launch receipt is missing or invalid")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("existing AC dev launch receipt is unreadable") from exc
    if not isinstance(receipt, Mapping):
        raise ValueError("existing AC dev launch receipt is invalid")
    binding = verified_stable_database_binding()
    _revalidate_stable_database_binding(binding)
    stable = _absolute_non_symlink_root(
        Path(str(binding.get("shared_volume_path") or "")), create=False
    )
    root_stat = root.stat(follow_symlinks=False)
    stable_stat = stable.stat(follow_symlinks=False)
    parent_stat = stable.parent.stat(follow_symlinks=False)
    required = {
        "schema_version": AC_DEV_LAUNCH_RECEIPT_SCHEMA,
        "world_id": AC_DEV_WORLD_ID,
        "project_id": AC_PROJECT_ID,
        "runtime_plane": DEV_RUNTIME_PLANE,
        "port": 40008,
        "background": False,
        "storage_root": str(root),
        "storage_device": int(root_stat.st_dev),
        "storage_inode": int(root_stat.st_ino),
        "stable_shared_volume": str(stable),
        "stable_shared_volume_device": int(stable_stat.st_dev),
        "stable_shared_volume_inode": int(stable_stat.st_ino),
        "stable_parent_device": int(parent_stat.st_dev),
        "stable_parent_inode": int(parent_stat.st_ino),
    }
    if any(receipt.get(key) != value for key, value in required.items()):
        raise ValueError("existing AC dev launch receipt mismatch")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("source_sha256") or "")):
        raise ValueError("existing AC dev launch receipt source hash is invalid")


def _recover_verified_existing_dev_sqlite(
    root: Path,
    database: Path,
    *,
    _after_checkpoint_for_test=None,
) -> None:
    """Bounded source-owned recovery for a stopped, verified dev world.

    No unlink is performed.  SQLite owns any WAL/SHM changes through a fully
    completed TRUNCATE checkpoint; errors leave source-tip/receipt state alone.
    """

    before = _sqlite_adoption_snapshot(root, database)
    if before["companions"].get("-journal") is not None:
        raise ValueError("AC dev SQLite adoption rejects rollback journal state")
    wal = Path(str(database) + "-wal")
    if wal.exists():
        wal_size = wal.stat(follow_symlinks=False).st_size
        if wal_size:
            header = wal.read_bytes()[:32]
            if len(header) != 32 or header[:4] not in (
                b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"
            ):
                raise ValueError("AC dev SQLite adoption WAL is malformed")
            page_size = int.from_bytes(header[8:12], "big")
            if page_size == 1:
                page_size = 65536
            if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
                raise ValueError("AC dev SQLite adoption WAL page size is malformed")
            if (wal_size - 32) % (page_size + 24):
                raise ValueError("AC dev SQLite adoption WAL frame layout is malformed")
    _assert_no_external_sqlite_holders(database)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(database), timeout=0, isolation_level=None)
        for pragma in ("integrity_check", "quick_check"):
            rows = [str(row[0]).lower() for row in conn.execute(f"PRAGMA {pragma}")]
            if rows != ["ok"]:
                raise ValueError(f"AC dev SQLite adoption {pragma} failed")
        result = tuple(int(value) for value in conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone())
        if result != (0, 0, 0):
            raise RuntimeError("AC dev SQLite adoption checkpoint is incomplete")
    except sqlite3.DatabaseError as exc:
        raise ValueError("AC dev SQLite adoption recovery failed") from exc
    finally:
        if conn is not None:
            conn.close()
    if _after_checkpoint_for_test is not None:
        _after_checkpoint_for_test()
    _assert_sqlite_adoption_identity(
        before, root, database, checkpoint_result=result
    )
    binding = verified_stable_database_binding()
    _revalidate_stable_database_binding(binding)


def _source_schema_table_contract() -> tuple[set[str], set[str], set[tuple[str, str, str]]]:
    """Return required tables and the exact baseline sqlite_master inventory."""

    with closing(sqlite3.connect(":memory:")) as memory:
        memory.row_factory = sqlite3.Row
        _configure_connection(memory, busy_timeout=10000)
        _ensure_schema(memory)
        required = {
            str(row[0])
            for row in memory.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        objects = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in memory.execute(
                "SELECT type, name, tbl_name FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'trigger', 'view')"
            )
        }
    return required, required | set(_DEV_SCHEMA_PRIMARY_KEYS), objects


def _source_schema_inventory_hash(conn: sqlite3.Connection, required: set[str]) -> str:
    placeholders = ",".join("?" for _ in required)
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        f"WHERE tbl_name IN ({placeholders}) ORDER BY type, name, tbl_name",
        tuple(sorted(required)),
    ).fetchall()
    return "sha256:" + hashlib.sha256(
        json.dumps([tuple(row) for row in rows], separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# This is deliberately a small, separately-versioned schema capability.  It is
# not a second general migration engine: the only admissible drift is a pristine
# absence of these named objects in an otherwise verified AC dev world.
BACKLOG_READ_SCHEMA_PLAN_VERSION = "ac_dev_backlog_read_schema_plan.v1"
BACKLOG_READ_SCHEMA_RESOURCE = "backlog"
BACKLOG_READ_SCHEMA_TABLE_DEFINITION = (
    (("resource", "TEXT", 0, None, 1), "resource TEXT PRIMARY KEY"),
    (("generation", "INTEGER", 1, "1", 0), "generation INTEGER NOT NULL DEFAULT 1"),
    (("updated_at", "TEXT", 1, "''", 0), "updated_at TEXT NOT NULL DEFAULT ''"),
)
BACKLOG_READ_SCHEMA_TABLE_XINFO = tuple(
    (*metadata, 0) for metadata, _sql in BACKLOG_READ_SCHEMA_TABLE_DEFINITION
)
BACKLOG_READ_SCHEMA_INDEX_SQL = (
    "CREATE INDEX idx_backlog_bugs_dashboard_keyset "
    "ON backlog_bugs(updated_at DESC, created_at DESC, bug_id DESC)"
)
BACKLOG_READ_SCHEMA_TABLE_SQL = (
    "CREATE TABLE dashboard_backlog_cache_generation ("
    + ", ".join(sql for _metadata, sql in BACKLOG_READ_SCHEMA_TABLE_DEFINITION)
    + ")"
)
BACKLOG_READ_SCHEMA_SEED_SQL = (
    "INSERT INTO dashboard_backlog_cache_generation "
    "(resource, generation, updated_at) VALUES ('backlog', 1, CURRENT_TIMESTAMP)"
)
BACKLOG_READ_SCHEMA_TRIGGER_SQL: Mapping[str, str] = {
    event: (
        f"CREATE TRIGGER trg_dashboard_backlog_cache_{event.lower()} "
        f"AFTER {event} ON backlog_bugs BEGIN "
        "UPDATE dashboard_backlog_cache_generation "
        "SET generation = generation + 1, updated_at = CURRENT_TIMESTAMP "
        "WHERE resource = 'backlog'; END"
    )
    for event in ("INSERT", "UPDATE", "DELETE")
}
BACKLOG_READ_SCHEMA_OBJECTS = frozenset({
    "dashboard_backlog_cache_generation",
    "idx_backlog_bugs_dashboard_keyset",
    *(f"trg_dashboard_backlog_cache_{event.lower()}" for event in BACKLOG_READ_SCHEMA_TRIGGER_SQL),
})

# This is a second, deliberately fixed admission capability.  It is not a
# migration framework: all definitions are exported by their owning modules,
# and an admission may only create a pristine absence of the complete set.
AC_AUTHORITY_SCHEMA_PLAN_VERSION = "ac_dev_authority_projection_schema_plan.v1"
AC_AUTHORITY_SCHEMA_INVENTORY_COUNT = 308
AC_AUTHORITY_SCHEMA_INVENTORY_SHA256 = (
    "sha256:76684bd3ece70dcae94e1b9678abf57ef74ca2d7dffdc4166414e5ca74cd7999"
)
AC_AUTHORITY_SCHEMA_TABLES = frozenset({
    "observer_route_token_refs",
    "contract_runtime_executions",
    "worker_implementation_test_results_corrections",
    "backlog_contract_chain_bindings",
    "contract_chain_edges",
    "backlog_contract_chain_current",
})


def _authority_projection_schema_statements() -> tuple[str, ...]:
    """Compose, but never duplicate, the six-owner authority DDL."""
    from .contracts.runtime import authority_projection_schema_statements
    from .observer_route_context import authority_route_registry_schema_statements
    return authority_route_registry_schema_statements() + authority_projection_schema_statements()


def _canonical_authority_projection_schema_inventory(*, include_plan: bool) -> tuple[tuple[str, str, str, str], ...]:
    with closing(sqlite3.connect(":memory:")) as memory:
        memory.row_factory = sqlite3.Row
        _configure_connection(memory, busy_timeout=10000)
        _ensure_schema(memory)
        if include_plan:
            for statement in _authority_projection_schema_statements():
                memory.execute(statement)
        return _sqlite_master_inventory(memory)


def authority_projection_schema_plan() -> dict[str, object]:
    statements = _authority_projection_schema_statements()
    return {
        "schema_version": AC_AUTHORITY_SCHEMA_PLAN_VERSION,
        "tables": tuple(sorted(AC_AUTHORITY_SCHEMA_TABLES)),
        "statements_sha256": "sha256:" + hashlib.sha256(
            json.dumps(statements, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def authority_projection_schema_inventory() -> dict[str, object]:
    """Return the exact full source-owned inventory for receipt preflight."""
    with closing(sqlite3.connect(":memory:")) as memory:
        memory.row_factory = sqlite3.Row
        _configure_connection(memory, busy_timeout=10000)
        _ensure_schema(memory)
        for statement in _authority_projection_schema_statements():
            memory.execute(statement)
        return backlog_read_schema_inventory(memory)


def authority_projection_schema_drift(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Fail closed unless this is exactly the six-object pristine absence."""
    base = _canonical_authority_projection_schema_inventory(include_plan=False)
    full = _canonical_authority_projection_schema_inventory(include_plan=True)
    actual = _sqlite_master_inventory(conn)
    base_map = {(k, n, t): s for k, n, t, s in base}
    full_map = {(k, n, t): s for k, n, t, s in full}
    actual_map = {(k, n, t): s for k, n, t, s in actual}
    invalid: list[str] = []
    for key in sorted(set(actual_map) - set(full_map)):
        invalid.append("inventory_extra:" + ":".join(key))
    for key in sorted(set(base_map) - set(actual_map)):
        invalid.append("inventory_missing:" + ":".join(key))
    for key in sorted(set(actual_map) & set(full_map)):
        if actual_map[key] != full_map[key]:
            invalid.append("inventory_altered:" + ":".join(key))
    plan_keys = set(full_map) - set(base_map)
    present = plan_keys & set(actual_map)
    if present and present != plan_keys:
        invalid.append("inventory_partial_plan")
    # A table can be absent while one of its auto indexes is necessarily also
    # absent.  sqlite_master inventories explicit objects; validate all PK and
    # UNIQUE autoindexes separately so an altered replacement cannot hide.
    for table in sorted(AC_AUTHORITY_SCHEMA_TABLES):
        if table in {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            expected = _DEV_SCHEMA_PRIMARY_KEYS.get(table)
            columns = tuple(str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})") if int(r[5]))
            if expected and columns != expected:
                invalid.append("primary_key_altered:" + table)
    missing = [] if present == plan_keys else sorted(AC_AUTHORITY_SCHEMA_TABLES)
    return {"missing": missing, "invalid": sorted(set(invalid))}


def admit_missing_authority_projection_schema(conn: sqlite3.Connection) -> dict[str, object]:
    """Create the complete authority namespace in one caller-owned txn."""
    before = authority_projection_schema_drift(conn)
    if before["invalid"]:
        raise ValueError("AC dev authority schema admission rejects invalid drift")
    if not before["missing"]:
        return {"changed": False, "missing": []}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _authority_projection_schema_statements():
            conn.execute(statement)
        after = authority_projection_schema_drift(conn)
        if after["missing"] or after["invalid"]:
            raise ValueError("AC dev authority schema admission postcondition failed")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"changed": True, "missing": before["missing"]}


def backlog_read_schema_plan() -> dict[str, object]:
    """Return the one canonical operator plan shared by stable/dev/CLI."""
    return {
        "schema_version": BACKLOG_READ_SCHEMA_PLAN_VERSION,
        "table": "dashboard_backlog_cache_generation",
        "index": "idx_backlog_bugs_dashboard_keyset",
        "triggers": tuple(sorted(BACKLOG_READ_SCHEMA_TRIGGER_SQL)),
        "allowed_objects": tuple(sorted(BACKLOG_READ_SCHEMA_OBJECTS)),
    }


def _backlog_read_normalized_sql(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().rstrip(";"))
    return re.sub(r"\s*([(),;])\s*", r"\1", normalized.replace(" IF NOT EXISTS ", " "))


def _sqlite_master_inventory(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    """Return the complete managed SQLite namespace, including SQL bodies.

    Names alone are not a schema authority: a shadow trigger or a table with an
    altered definition can change the meaning of the same read path.  SQLite's
    internal autoindexes do not have sqlite_master SQL rows and are deliberately
    outside this source-derived inventory.
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "ORDER BY type, name, tbl_name"
    ).fetchall()
    return tuple(
        (str(kind), str(name), str(table), _backlog_read_normalized_sql(sql))
        for kind, name, table, sql in rows
    )


def _canonical_backlog_read_schema_inventory(*, include_plan: bool) -> tuple[tuple[str, str, str, str], ...]:
    """Materialize the source ABI in memory; never infer it from the target."""
    with closing(sqlite3.connect(":memory:")) as memory:
        memory.row_factory = sqlite3.Row
        _configure_connection(memory, busy_timeout=10000)
        _ensure_schema(memory)
        if include_plan:
            memory.execute(BACKLOG_READ_SCHEMA_TABLE_SQL)
            memory.execute(BACKLOG_READ_SCHEMA_INDEX_SQL)
            memory.execute(BACKLOG_READ_SCHEMA_SEED_SQL)
            for sql in BACKLOG_READ_SCHEMA_TRIGGER_SQL.values():
                memory.execute(sql)
        return _sqlite_master_inventory(memory)


def backlog_read_schema_inventory(conn: sqlite3.Connection) -> dict[str, object]:
    """Return a content-addressed complete inventory for receipt binding."""
    inventory = _sqlite_master_inventory(conn)
    encoded = json.dumps(inventory, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {
        # Receipts are JSON, so keep the public value JSON-native as well;
        # otherwise a parsed receipt (lists) cannot equal a fresh observation
        # (tuples) despite identical canonical bytes.
        "inventory": [list(item) for item in inventory],
        "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _backlog_read_managed_object_names() -> frozenset[str]:
    """The only sqlite_master names an offline backlog admission may create."""
    return frozenset({
        "dashboard_backlog_cache_generation",
        "idx_backlog_bugs_dashboard_keyset",
        *(f"trg_dashboard_backlog_cache_{event.lower()}"
          for event in BACKLOG_READ_SCHEMA_TRIGGER_SQL),
    })


def _schema_inventory_binding(
    rows: tuple[tuple[str, str, str, str], ...], *, hash_sql: bool,
) -> dict[str, object]:
    """Serialize a deterministic SQLite inventory without trusting names alone."""
    values = tuple(
        (kind, name, table, "sha256:" + hashlib.sha256(sql.encode("utf-8")).hexdigest())
        if hash_sql else (kind, name, table, sql)
        for kind, name, table, sql in rows
    )
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {
        "inventory": [list(item) for item in values],
        "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def backlog_read_schema_managed_inventory(conn: sqlite3.Connection) -> dict[str, object]:
    """Bind exactly the five source-owned backlog-read objects, and no others."""
    names = _backlog_read_managed_object_names()
    return _schema_inventory_binding(
        tuple(row for row in _sqlite_master_inventory(conn) if row[1] in names),
        hash_sql=False,
    )


def canonical_backlog_read_schema_managed_inventory() -> dict[str, object]:
    """Return the exact post-admission managed-object binding without a target DB."""
    names = _backlog_read_managed_object_names()
    return _schema_inventory_binding(
        tuple(
            row for row in _canonical_backlog_read_schema_inventory(include_plan=True)
            if row[1] in names
        ),
        hash_sql=False,
    )


def backlog_read_schema_protected_inventory(conn: sqlite3.Connection) -> dict[str, object]:
    """Bind every non-managed object before an offline admission mutates sqlite."""
    names = _backlog_read_managed_object_names()
    return _schema_inventory_binding(
        tuple(
            row for row in _sqlite_master_inventory(conn)
            if row[1] not in names
            and not row[1].startswith("sqlite_autoindex_dashboard_backlog_cache_generation_")
        ),
        hash_sql=True,
    )


def backlog_read_schema_drift(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Classify the bounded backlog-read plan without writing.

    ``missing`` is the *only* class the offline admission may repair.  Any
    definition mismatch, partial seed, or unexpected similarly-named object is
    hostile drift and must remain a zero-write rejection.
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name IN (%s)" % ",".join("?" for _ in BACKLOG_READ_SCHEMA_OBJECTS),
        tuple(sorted(BACKLOG_READ_SCHEMA_OBJECTS)),
    ).fetchall()
    actual = {str(row[1]): tuple(row) for row in rows}
    missing: list[str] = []
    invalid: list[str] = []
    table = actual.get("dashboard_backlog_cache_generation")
    if table is None:
        missing.append("generation_table")
    elif table[0] != "table" or _backlog_read_normalized_sql(table[3]) != _backlog_read_normalized_sql(BACKLOG_READ_SCHEMA_TABLE_SQL):
        invalid.append("generation_table")
        invalid.append("inventory_altered:table:dashboard_backlog_cache_generation")
    index = actual.get("idx_backlog_bugs_dashboard_keyset")
    if index is None:
        missing.append("keyset_index")
    elif index[0] != "index" or index[2] != "backlog_bugs" or _backlog_read_normalized_sql(index[3]) != _backlog_read_normalized_sql(BACKLOG_READ_SCHEMA_INDEX_SQL):
        invalid.append("keyset_index")
        invalid.append("inventory_altered:index:idx_backlog_bugs_dashboard_keyset")
    for event, sql in BACKLOG_READ_SCHEMA_TRIGGER_SQL.items():
        name = f"trg_dashboard_backlog_cache_{event.lower()}"
        trigger = actual.get(name)
        if trigger is None:
            missing.append(f"trigger_{event.lower()}")
        elif trigger[0] != "trigger" or trigger[2] != "backlog_bugs" or _backlog_read_normalized_sql(trigger[3]) != _backlog_read_normalized_sql(sql):
            invalid.append(f"trigger_{event.lower()}")
            invalid.append("inventory_altered:trigger:" + name)
    if table is not None and "generation_table" not in invalid:
        seeds = conn.execute(
            "SELECT resource, generation, updated_at FROM dashboard_backlog_cache_generation "
            "WHERE resource=?", (BACKLOG_READ_SCHEMA_RESOURCE,)
        ).fetchall()
        if not seeds:
            missing.append("backlog_generation_seed")
        elif len(seeds) != 1 or str(seeds[0][0]) != BACKLOG_READ_SCHEMA_RESOURCE or int(seeds[0][1]) < 1 or not str(seeds[0][2]):
            invalid.append("backlog_generation_seed")
    # This admission owns only the bounded backlog-read namespace.  Runtime,
    # observer, worker, and contract objects are protected by the caller's
    # complete inventory/projection binding; treating them as backlog drift
    # would incorrectly force a pristine whole-world database.
    managed_names = _backlog_read_managed_object_names()
    base_names = {name for _kind, name, _table, _sql in _canonical_backlog_read_schema_inventory(include_plan=False)}
    for kind, name, table_name, _sql in _sqlite_master_inventory(conn):
        # Any collision/shadow object that claims the managed backlog naming
        # domain remains fail-closed; unrelated objects do not.
        if name in managed_names or name.startswith("sqlite_autoindex_dashboard_backlog_cache_generation_"):
            continue
        if name not in base_names and ("dashboard" in name.lower() or name.startswith("shadow_backlog")):
            invalid.append("inventory_extra:" + ":".join((kind, name, table_name)))
    # A partially materialized managed object set is never repairable: only
    # the pristine absence of all five objects may be admitted.
    if any(name in actual for name in managed_names) and missing:
        invalid.append("inventory_partial_plan")
    return {"missing": sorted(set(missing)), "invalid": sorted(set(invalid))}


def ensure_backlog_read_schema(conn: sqlite3.Connection) -> None:
    """Stable-only initializer for the bounded backlog-read plan."""
    conn.execute(BACKLOG_READ_SCHEMA_TABLE_SQL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    conn.execute(BACKLOG_READ_SCHEMA_INDEX_SQL.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))
    conn.execute(BACKLOG_READ_SCHEMA_SEED_SQL.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1))
    for sql in BACKLOG_READ_SCHEMA_TRIGGER_SQL.values():
        conn.execute(sql.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1))
    conn.commit()


def admit_missing_backlog_read_schema(
    conn: sqlite3.Connection, *, commit: bool = True,
) -> dict[str, object]:
    """Apply exactly the known missing-object set in one caller-owned transaction."""
    before = backlog_read_schema_drift(conn)
    if before["invalid"]:
        raise ValueError("AC dev schema admission rejects invalid backlog-read drift")
    if not before["missing"]:
        return {"changed": False, "missing": []}
    if commit:
        conn.execute("BEGIN IMMEDIATE")
    elif not conn.in_transaction:
        raise ValueError("caller-owned backlog-read admission requires an active transaction")
    try:
        # The preflight allows only pristine absences; each statement is
        # unconditional so a concurrent/create race becomes a rollback.
        if "generation_table" in before["missing"]:
            conn.execute(BACKLOG_READ_SCHEMA_TABLE_SQL)
        if "keyset_index" in before["missing"]:
            conn.execute(BACKLOG_READ_SCHEMA_INDEX_SQL)
        if (
            "generation_table" in before["missing"]
            or "backlog_generation_seed" in before["missing"]
        ):
            conn.execute(BACKLOG_READ_SCHEMA_SEED_SQL)
        for event, sql in BACKLOG_READ_SCHEMA_TRIGGER_SQL.items():
            if f"trigger_{event.lower()}" in before["missing"]:
                conn.execute(sql)
        after = backlog_read_schema_drift(conn)
        if after["missing"] or after["invalid"]:
            raise ValueError("AC dev schema admission postcondition failed")
        if commit:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"changed": True, "missing": before["missing"]}


def _verify_dev_world_schema_inventory(conn: sqlite3.Connection) -> None:
    required, allowed, source_objects = _source_schema_table_contract()
    actual = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    actual_objects = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
    }
    managed_exact = (
        backlog_read_schema_managed_inventory(conn)
        == canonical_backlog_read_schema_managed_inventory()
    )
    managed_table = "dashboard_backlog_cache_generation"
    managed_names = _backlog_read_managed_object_names()
    managed_autoindex = "sqlite_autoindex_dashboard_backlog_cache_generation_1"
    # This exception is deliberately all-or-nothing: the SQL-bearing five
    # objects must equal the source plan before *only* their exact namespace
    # can be removed from the baseline source-inventory comparison.
    accepted_overlay = set()
    if managed_exact:
        accepted_overlay = {
            (kind, name, table)
            for kind, name, table in actual_objects
            if name in managed_names
            or (kind == "index" and name == managed_autoindex and table == managed_table)
        }
        if ("table", managed_table, managed_table) not in accepted_overlay or (
                "index", managed_autoindex, managed_table) not in accepted_overlay:
            accepted_overlay = set()
            managed_exact = False
    unknown = sorted((actual - allowed) - ({managed_table} if managed_exact else set()))
    missing = sorted(required - actual)
    optional = allowed - required
    unknown_objects = sorted(
        item
        for item in actual_objects - source_objects - accepted_overlay
        if not (item[0] in {"table", "index"} and item[2] in optional)
    )
    if unknown or missing or unknown_objects:
        raise ValueError(
            "AC dev source schema inventory mismatch: "
            f"unknown={unknown}, missing={missing}, unknown_objects={unknown_objects}"
        )


def _durable_database_sha256(database: Path) -> str:
    """Hash one stable, no-follow database inode."""
    before = database.stat(follow_symlinks=False)
    if database.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError("AC dev durable database is not a canonical regular file")
    descriptor = os.open(database, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise ValueError("AC dev durable database changed during hashing")
    return "sha256:" + digest.hexdigest()


def _cow_regular_identity(path: Path, *, nlink: int | None = None) -> dict[str, object]:
    absolute = path.expanduser().absolute()
    metadata = absolute.stat(follow_symlinks=False)
    if (absolute.is_symlink() or not stat.S_ISREG(metadata.st_mode)
            or absolute.resolve(strict=True) != absolute
            or (nlink is not None and int(metadata.st_nlink) != nlink)):
        raise ValueError("AC dev COW successor artifact is not canonical")
    return {"path": str(absolute), "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino), "size": int(metadata.st_size),
            "nlink": int(metadata.st_nlink), "sha256": _durable_database_sha256(absolute)}


def _cow_raw_receipt(path: Path, *, prefix: str = "") -> tuple[dict[str, object], str]:
    absolute = path.expanduser().absolute()
    metadata = absolute.stat(follow_symlinks=False)
    if (absolute.is_symlink() or not stat.S_ISREG(metadata.st_mode)
            or int(metadata.st_nlink) != 1 or absolute.resolve(strict=True) != absolute):
        raise ValueError("AC dev COW successor evidence is not canonical")
    raw = absolute.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if prefix:
        if (re.fullmatch(rf"{re.escape(prefix)}\.([0-9a-f]{{64}})\.json", absolute.name) is None
                or absolute.name != f"{prefix}.{digest}.json"):
            raise ValueError("AC dev COW successor receipt digest mismatch")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("AC dev COW successor evidence is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("AC dev COW successor evidence is invalid")
    return dict(value), "sha256:" + digest


def _cow_database_observation(
    database: Path, *, expected_rows: int = 3603, require_managed: bool = True,
    nlink: int | None = 1,
) -> dict[str, object]:
    identity = _cow_regular_identity(database, nlink=nlink)
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(database) + suffix).exists() or Path(str(database) + suffix).is_symlink():
            raise ValueError("AC dev COW successor database has sidecars")
    _assert_no_external_sqlite_holders(database)
    uri = "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("AC dev COW successor quick-check failed")
        # Root/genesis/receipt/service validation already binds this complete
        # SQLite world to aming-claw.  backlog_bugs itself intentionally has no
        # project_id column, so authenticate its exact source-owned ABI before
        # reading the whole project-bound table.
        with closing(sqlite3.connect(":memory:")) as canonical:
            canonical.row_factory = sqlite3.Row
            _configure_connection(canonical, busy_timeout=10000)
            _ensure_schema(canonical)
            expected_sql_row = canonical.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='backlog_bugs'"
            ).fetchone()
            expected_columns = [tuple(row) for row in canonical.execute(
                'PRAGMA table_info("backlog_bugs")'
            )]
            if require_managed:
                ensure_backlog_read_schema(canonical)
            expected_required, _expected_allowed, _expected_objects = _source_schema_table_contract()
            expected_placeholders = ",".join("?" for _ in expected_required)
            expected_source_inventory = [tuple(row) for row in canonical.execute(
                "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
                f"WHERE tbl_name IN ({expected_placeholders}) ORDER BY type,name,tbl_name",
                tuple(sorted(expected_required)),
            )]
            expected_source_sha = _source_schema_inventory_hash(canonical, expected_required)
        actual_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='backlog_bugs'"
        ).fetchone()
        actual_columns = [tuple(row) for row in conn.execute(
            'PRAGMA table_info("backlog_bugs")'
        )]
        if (expected_sql_row is None or actual_sql_row is None
                or _backlog_read_normalized_sql(actual_sql_row[0])
                != _backlog_read_normalized_sql(expected_sql_row[0])
                or actual_columns != expected_columns):
            raise ValueError("AC dev COW successor backlog table ABI mismatch")
        meta = dict(conn.execute("SELECT key, value FROM schema_meta"))
        row_count = int(conn.execute("SELECT COUNT(*) FROM backlog_bugs").fetchone()[0])
        status_counts = {
            str(status): int(count) for status, count in conn.execute(
                "SELECT status,COUNT(*) FROM backlog_bugs GROUP BY status ORDER BY status"
            )
        }
        managed = backlog_read_schema_managed_inventory(conn)
        canonical_managed = canonical_backlog_read_schema_managed_inventory()
        drift = backlog_read_schema_drift(conn)
        protected_inventory = backlog_read_schema_protected_inventory(conn)
        protected_projection = _sqlite_logical_projection(
            conn, exclude_tables=frozenset({"backlog_bugs", "dashboard_backlog_cache_generation"}),
        )
        backlog_projection = _sqlite_logical_projection(conn).get("backlog_bugs")
        required_tables, _allowed_tables, _source_objects = _source_schema_table_contract()
        placeholders = ",".join("?" for _ in required_tables)
        source_inventory = [tuple(row) for row in conn.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            f"WHERE tbl_name IN ({placeholders}) ORDER BY type,name,tbl_name",
            tuple(sorted(required_tables)),
        )]
        source_schema = {
            "required_tables": sorted(required_tables),
            "inventory": [list(row) for row in source_inventory],
            "sha256": _source_schema_inventory_hash(conn, required_tables),
        }
        _verify_dev_world_schema_inventory(conn)
    finally:
        conn.close()
    if (row_count != expected_rows
            or (require_managed and managed != canonical_managed)
            or (require_managed and drift.get("invalid"))
            or source_inventory != expected_source_inventory
            or source_schema["sha256"] != expected_source_sha):
        raise ValueError("AC dev COW successor backlog projection is invalid")
    return {"identity": identity, "quick_check": "ok", "row_count": row_count,
            "status_counts": status_counts, "managed_inventory": managed,
            "managed_inventory_drift": [], "protected_inventory": protected_inventory,
            "protected_projection": protected_projection,
            "backlog_projection_sha256": backlog_projection,
            "source_schema": source_schema,
            "governance_world_id": str(meta.get("governance_world_id") or ""),
            "genesis_json": str(meta.get("governance_world_genesis_json") or ""),
            "genesis_sha256": str(meta.get("governance_world_genesis_sha256") or "")}


def _cow_successor_archive(root: Path) -> Path:
    return root / AC_DEV_COW_SUCCESSOR_ARCHIVE


def create_dev_cow_successor_receipt(
    storage_root: Path | str, *, operator_receipt: Path, predecessor_backup: Path,
    linked_v3_receipt: Path,
) -> dict[str, object]:
    """Create the sole immutable authority for an operator-supervised DB COW."""
    root = Path(storage_root).expanduser().absolute()
    if root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("AC dev COW successor root is invalid")
    listener = _default_cutover_listener_probe(40008)
    if listener.get("listening") or int(listener.get("pid") or 0):
        raise ValueError("AC dev COW successor requires stopped port 40008")
    database = root / AC_DATABASE_DEV_RELATIVE_PATH
    successor = _cow_database_observation(database)
    backup_path = predecessor_backup.expanduser().absolute()
    backup = _cow_regular_identity(backup_path)
    if backup_path.parent != root / "archive" / "operator-exception-backups":
        raise ValueError("AC dev COW successor backup is outside its archive")
    backup_observation = _cow_database_observation(
        backup_path, expected_rows=0, require_managed=False, nlink=None,
    )
    operator_path = operator_receipt.expanduser().absolute()
    operator, operator_sha = _cow_raw_receipt(operator_path, prefix="cow-import")
    if operator_path.parent != root / "archive" / "operator-exceptions":
        raise ValueError("AC dev COW successor operator evidence is outside its archive")
    if (operator.get("schema_version") != "ac_dev_operator_exception_cow_import.v1"
            or operator.get("qa_pass") is not False or operator.get("release_authority") is not False
            or operator.get("rows") != 3603 or not operator.get("decisions")
            or operator.get("backup_sha256") != backup["sha256"]
            or operator.get("target_sha256_before") != backup["sha256"]
            or operator.get("target_sha256_after") != successor["identity"]["sha256"]):
        raise ValueError("AC dev COW successor operator evidence mismatch")
    if (backup_observation["governance_world_id"] != AC_DEV_WORLD_ID
            or successor["governance_world_id"] != AC_DEV_WORLD_ID
            or backup_observation["genesis_json"] != successor["genesis_json"]
            or backup_observation["genesis_sha256"] != successor["genesis_sha256"]
            or backup_observation["protected_inventory"] != successor["protected_inventory"]
            or backup_observation["protected_projection"] != successor["protected_projection"]):
        raise ValueError("AC dev COW successor protected preimage mismatch")
    try:
        genesis = json.loads(str(successor["genesis_json"]))
    except ValueError as exc:
        raise ValueError("AC dev COW successor genesis is unreadable") from exc
    canonical_genesis_raw = json.dumps(
        dict(genesis), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ) if isinstance(genesis, Mapping) else ""
    rows_copied = genesis.get("rows_copied") if isinstance(genesis, Mapping) else None
    if (not isinstance(genesis, Mapping)
            or genesis.get("schema_version") != AC_WORLD_GENESIS_SCHEMA
            or genesis.get("world_id") != AC_DEV_WORLD_ID
            or genesis.get("project_id") != AC_PROJECT_ID
            or genesis.get("source_only") is not True
            or not isinstance(rows_copied, int) or isinstance(rows_copied, bool)
            or rows_copied != 0
            or successor["genesis_json"] != canonical_genesis_raw
            or successor["genesis_sha256"] != _world_genesis_hash(genesis)
            or dict(genesis.get("database_identity") or {}).get("device") != backup["device"]
            or dict(genesis.get("database_identity") or {}).get("inode") != backup["inode"]):
        raise ValueError("AC dev COW successor predecessor genesis mismatch")
    linked_path = linked_v3_receipt.expanduser().absolute()
    linked, linked_sha = _cow_raw_receipt(linked_path)
    if linked_path.parent != root / "archive" / "schema-admission":
        raise ValueError("AC dev COW successor linked receipt is outside its archive")
    linked_hex = linked_sha.removeprefix("sha256:")
    linked_sidecar = linked_path.with_suffix(".sha256")
    if (linked_path.name != f"{linked_hex}.json" or linked_sidecar.is_symlink()
            or not linked_sidecar.is_file()
            or linked_sidecar.read_text(encoding="utf-8")
            != f"sha256:{linked_hex}  {linked_path.name}\n"):
        raise ValueError("AC dev COW successor linked receipt sidecar mismatch")
    adoptions = sorted((root / "archive" / "canonical-legacy-postimage-adoption").glob("adoption.*.json"))
    if len(adoptions) != 1:
        raise ValueError("AC dev COW successor adoption evidence is missing or ambiguous")
    adoption, adoption_sha = _cow_raw_receipt(adoptions[0], prefix="adoption")
    if (adoption.get("schema_version") != "ac_dev_canonical_legacy_postimage_adoption.v1"
            or adoption.get("stage") != "completed"
            or adoption.get("project_id") != AC_PROJECT_ID or adoption.get("port") != 40008
            or linked.get("schema_version") != "ac_dev_offline_schema_admission.v3"
            or linked.get("stage") != "completed"
            or adoption.get("linked_v3_receipt") != str(linked_path)
            or adoption.get("linked_v3_receipt_sha256") != linked_sha
            or linked.get("database_identity", {}).get("device") != backup["device"]
            or linked.get("database_identity", {}).get("inode") != backup["inode"]):
        raise ValueError("AC dev COW successor historical chain mismatch")
    stable = verified_stable_database_binding()
    _revalidate_stable_database_binding(stable)
    stable_db = Path(str(stable["database_path"]))
    stable_metadata = stable_db.stat(follow_symlinks=False)
    stable_identity = dict(stable["stable_database_identity"])
    if (stable_db.is_symlink() or not stat.S_ISREG(stable_metadata.st_mode)
            or (stable_identity["device"], stable_identity["inode"]) == (
                successor["identity"]["device"], successor["identity"]["inode"])):
        raise ValueError("AC dev COW successor aliases stable database")
    payload = {"schema_version": AC_DEV_COW_SUCCESSOR_SCHEMA, "stage": "completed",
               "project_id": AC_PROJECT_ID, "port": 40008, "root": str(root),
               "listener": {"host": "127.0.0.1", "port": 40008, "listening": False},
               "genesis": {"raw_json": successor["genesis_json"],
                           "sha256": successor["genesis_sha256"]},
               "predecessor": {"backup": backup}, "successor": successor,
               "operator_evidence": {"path": str(operator_path), "sha256": operator_sha,
                                     "payload": operator},
               "history": {"linked_v3": {"path": str(linked_path), "sha256": linked_sha},
                           "adoption": {"path": str(adoptions[0]), "sha256": adoption_sha}},
               "stable_binding": {"database": stable_identity,
                                  "runtime_commit": stable.get("commit")}}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    archive = _cow_successor_archive(root)
    archive.mkdir(parents=True, exist_ok=True)
    if archive.is_symlink() or archive.resolve(strict=True) != archive:
        raise ValueError("AC dev COW successor archive is invalid")
    existing = sorted(archive.glob(f"{AC_DEV_COW_SUCCESSOR_PREFIX}.*.json"))
    destination = archive / f"{AC_DEV_COW_SUCCESSOR_PREFIX}.{digest}.json"
    if existing:
        if existing != [destination] or destination.read_bytes() != raw:
            raise ValueError("AC dev COW successor receipt is ambiguous")
        return {"receipt": str(destination), "receipt_sha256": "sha256:" + digest,
                "status": "already_created"}
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o444)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(archive, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"receipt": str(destination), "receipt_sha256": "sha256:" + digest,
            "status": "created"}


def _cow_historical_backlog_snapshot(
    root: Path, operator: Mapping[str, object]
) -> tuple[dict[str, object], list[tuple[object, ...]], list[str]]:
    snapshot_sha = str(operator.get("snapshot_sha256") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_sha):
        raise ValueError("AC dev COW successor historical snapshot authority is invalid")
    archive = root / "archive" / "staging" / "dashboard-backlog-snapshots"
    snapshot = archive / f"snapshot.{snapshot_sha.removeprefix('sha256:')}.sqlite"
    identity = _cow_regular_identity(snapshot)
    if identity["sha256"] != snapshot_sha:
        raise ValueError("AC dev COW successor historical snapshot hash mismatch")
    manifests = sorted(archive.glob("manifest.*.json"))
    if len(manifests) != 1:
        raise ValueError("AC dev COW successor historical snapshot manifest is ambiguous")
    manifest, manifest_sha = _cow_raw_receipt(manifests[0], prefix="manifest")
    uri = "file:" + urllib.parse.quote(str(snapshot)) + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("AC dev COW successor historical snapshot is corrupt")
        columns = [str(row[1]) for row in conn.execute('PRAGMA table_info("backlog_bugs")')]
        quoted = ",".join(_sqlite_quote_identifier(name) for name in columns)
        rows = [tuple(row) for row in conn.execute(
            f'SELECT {quoted} FROM "backlog_bugs" ORDER BY "bug_id"'
        )]
        status_index = columns.index("status")
        statuses: dict[str, int] = {}
        for row in rows:
            status = str(row[status_index])
            statuses[status] = statuses.get(status, 0) + 1
        backlog_projection = _sqlite_logical_projection(conn).get("backlog_bugs")
    finally:
        conn.close()
    if (
        manifest.get("schema_version") != "ac_dev_dashboard_backlog_snapshot.v1"
        or manifest.get("destination_path") != str(snapshot)
        or manifest.get("destination_sha256") != snapshot_sha
        or manifest.get("row_count") != len(rows)
        or manifest.get("status_counts") != dict(sorted(statuses.items()))
        or manifest_sha != "sha256:" + manifests[0].stem.removeprefix("manifest.")
    ):
        raise ValueError("AC dev COW successor historical snapshot manifest mismatch")
    return {
        "row_count": len(rows),
        "status_counts": dict(sorted(statuses.items())),
        "backlog_projection_sha256": backlog_projection,
    }, rows, columns


def _reconstruct_dev_cow_successor_payload(
    root: Path, receipt: Mapping[str, object]
) -> dict[str, object]:
    legacy_paths = sorted(_cow_successor_archive(root).glob("successor.*.json"))
    if len(legacy_paths) != 1:
        raise ValueError("AC dev COW successor historical v1 authority is missing or ambiguous")
    legacy, _legacy_sha = _cow_raw_receipt(legacy_paths[0], prefix="successor")
    operator_ref = dict(receipt.get("operator_evidence") or {})
    operator_path = Path(str(operator_ref.get("path") or ""))
    operator, operator_sha = _cow_raw_receipt(operator_path, prefix="cow-import")
    predecessor_ref = dict(dict(receipt.get("predecessor") or {}).get("backup") or {})
    predecessor_path = Path(str(predecessor_ref.get("path") or ""))
    predecessor = _cow_regular_identity(predecessor_path)
    history_ref = dict(receipt.get("history") or {})
    linked_ref = dict(history_ref.get("linked_v3") or {})
    adoption_ref = dict(history_ref.get("adoption") or {})
    linked_path = Path(str(linked_ref.get("path") or ""))
    adoption_path = Path(str(adoption_ref.get("path") or ""))
    linked, linked_sha = _cow_raw_receipt(linked_path)
    adoption, adoption_sha = _cow_raw_receipt(adoption_path, prefix="adoption")
    quarantine_ref = dict(adoption.get("quarantine_manifest") or {})
    quarantine_path = Path(str(quarantine_ref.get("path") or ""))
    _quarantine, quarantine_sha = _cow_raw_receipt(quarantine_path, prefix="manifest")
    if (
        legacy.get("schema_version") != "ac_dev_cow_database_successor.v1"
        or legacy.get("stage") != "completed"
        or legacy.get("project_id") != AC_PROJECT_ID
        or legacy.get("port") != 40008
        or legacy.get("root") != str(root)
        or legacy.get("listener")
        != {"host": "127.0.0.1", "port": 40008, "listening": False}
        or legacy.get("predecessor") != receipt.get("predecessor")
        or legacy.get("operator_evidence") != receipt.get("operator_evidence")
        or legacy.get("history") != receipt.get("history")
        or operator_path.parent != root / "archive" / "operator-exceptions"
        or predecessor_path.parent != root / "archive" / "operator-exception-backups"
        or linked_path.parent != root / "archive" / "schema-admission"
        or adoption_path.parent != root / "archive" / "canonical-legacy-postimage-adoption"
        or operator_ref != {"path": str(operator_path), "sha256": operator_sha, "payload": operator}
        or linked_ref != {"path": str(linked_path), "sha256": linked_sha}
        or adoption_ref != {"path": str(adoption_path), "sha256": adoption_sha}
        or quarantine_ref != {"path": str(quarantine_path), "sha256": quarantine_sha}
        or adoption.get("linked_v3_receipt") != str(linked_path)
        or adoption.get("linked_v3_receipt_sha256") != linked_sha
        or operator.get("schema_version") != "ac_dev_operator_exception_cow_import.v1"
        or operator.get("qa_pass") is not False
        or operator.get("release_authority") is not False
        or not operator.get("decisions")
        or operator.get("backup_sha256") != predecessor["sha256"]
        or operator.get("target_sha256_before") != predecessor["sha256"]
        or linked.get("schema_version") != "ac_dev_offline_schema_admission.v3"
        or linked.get("stage") != "completed"
        or linked.get("project_id") != AC_PROJECT_ID
        or linked.get("port") != 40008
        or dict(linked.get("database_identity") or {}).get("device")
        != predecessor["device"]
        or dict(linked.get("database_identity") or {}).get("inode")
        != predecessor["inode"]
        or adoption.get("schema_version")
        != "ac_dev_canonical_legacy_postimage_adoption.v1"
        or adoption.get("stage") != "completed"
        or adoption.get("project_id") != AC_PROJECT_ID
        or adoption.get("port") != 40008
        or quarantine_path.parent.parent
        != root / "quarantine" / "schema-admission-sidecars"
        or _quarantine.get("schema_version")
        != "aming-claw.schema-admission-sidecar-quarantine.v1"
        or _quarantine.get("stage") != "completed"
    ):
        raise ValueError("AC dev COW successor historical artifact chain mismatch")
    linked_sidecar = linked_path.with_suffix(".sha256")
    if (
        linked_sidecar.is_symlink()
        or not linked_sidecar.is_file()
        or linked_sidecar.read_text(encoding="utf-8")
        != f"{linked_sha}  {linked_path.name}\n"
    ):
        raise ValueError("AC dev COW successor historical linked receipt mismatch")
    snapshot, rows, columns = _cow_historical_backlog_snapshot(root, operator)

    descriptor, temporary_name = tempfile.mkstemp(prefix="ac-cow-reconstruct-", suffix=".sqlite")
    os.close(descriptor)
    temporary = Path(temporary_name).resolve(strict=True)
    try:
        shutil.copyfile(predecessor_path, temporary)
        os.chmod(temporary, 0o600)
        conn = sqlite3.connect(str(temporary), isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            admit_missing_backlog_read_schema(conn, commit=False)
            quoted = ",".join(_sqlite_quote_identifier(name) for name in columns)
            placeholders = ",".join("?" for _ in columns)
            conn.executemany(
                f'INSERT INTO "backlog_bugs" ({quoted}) VALUES ({placeholders})', rows
            )
            conn.commit()
            if tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) != (0, 0, 0):
                raise ValueError("AC dev COW successor reconstruction checkpoint failed")
        finally:
            conn.close()
        reconstructed = _cow_database_observation(temporary)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(str(temporary) + suffix).unlink()
            except FileNotFoundError:
                pass
    historical_successor = dict(legacy.get("successor") or {})
    historical_identity = dict(historical_successor.get("identity") or {})
    if (
        set(historical_identity) != {"path", "device", "inode", "size", "nlink", "sha256"}
        or historical_identity.get("path") != str(root / AC_DATABASE_DEV_RELATIVE_PATH)
        or historical_identity.get("sha256") != operator.get("target_sha256_after")
        or historical_identity.get("size") != reconstructed["identity"]["size"]
    ):
        raise ValueError("AC dev COW successor historical v1 database authority mismatch")
    reconstructed["identity"] = historical_identity
    reconstructed.update(snapshot)
    stable_binding = dict(legacy.get("stable_binding") or {})
    stable_identity = dict(stable_binding.get("database") or {})
    if (
        set(stable_binding) != {"database", "runtime_commit"}
        or set(stable_identity)
        != {"schema_version", "device", "inode", "stable_relative_path_sha256"}
        or stable_identity.get("schema_version") != "ac_stable_database_identity.v1"
        or (stable_identity.get("device"), stable_identity.get("inode"))
        == (historical_identity.get("device"), historical_identity.get("inode"))
    ):
        raise ValueError("AC dev COW successor historical v1 stable authority mismatch")
    return {
        "schema_version": AC_DEV_COW_SUCCESSOR_SCHEMA, "stage": "completed",
        "project_id": AC_PROJECT_ID, "port": 40008, "root": str(root),
        "listener": dict(legacy["listener"]),
        "genesis": {"raw_json": reconstructed["genesis_json"],
                    "sha256": reconstructed["genesis_sha256"]},
        "predecessor": {"backup": predecessor}, "successor": reconstructed,
        "operator_evidence": {"path": str(operator_path), "sha256": operator_sha,
                              "payload": operator},
        "history": {"linked_v3": {"path": str(linked_path), "sha256": linked_sha},
                    "adoption": {"path": str(adoption_path), "sha256": adoption_sha}},
        "stable_binding": stable_binding,
    }


def validate_dev_cow_successor_receipt(storage_root: Path | str) -> dict[str, object]:
    """Revalidate immutable v2 issuance evidence, never mutable live rows."""
    root = Path(storage_root).expanduser().absolute()
    archive = _cow_successor_archive(root)
    receipts = sorted(archive.glob(f"{AC_DEV_COW_SUCCESSOR_PREFIX}.*.json")) if archive.is_dir() else []
    if len(receipts) != 1:
        raise ValueError("AC dev COW successor receipt is missing or ambiguous")
    receipt, digest = _cow_raw_receipt(receipts[0], prefix=AC_DEV_COW_SUCCESSOR_PREFIX)
    if (receipt.get("schema_version") != AC_DEV_COW_SUCCESSOR_SCHEMA
            or receipt.get("stage") != "completed" or receipt.get("project_id") != AC_PROJECT_ID
            or receipt.get("port") != 40008 or receipt.get("root") != str(root)):
        raise ValueError("AC dev COW successor receipt mismatch")
    expected = _reconstruct_dev_cow_successor_payload(root, receipt)
    receipt_canonical = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    expected_canonical = json.dumps(
        expected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if receipt_canonical != expected_canonical:
        raise ValueError("AC dev COW successor reconstructed issuance payload mismatch")
    return receipt

def _verify_current_dev_backlog_runtime_invariants(
    conn: sqlite3.Connection, *, expected_protected_inventory: Mapping[str, object]
) -> None:
    """Validate mutable backlog state through schema/generation invariants."""

    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("existing AC dev world quick-check failed")
    drift = backlog_read_schema_drift(conn)
    if drift.get("missing") or drift.get("invalid"):
        raise ValueError(
            "existing AC dev backlog storage managed generation is invalid"
        )
    if (
        backlog_read_schema_managed_inventory(conn)
        != canonical_backlog_read_schema_managed_inventory()
    ):
        raise ValueError("existing AC dev backlog managed inventory changed")
    if (
        backlog_read_schema_protected_inventory(conn)
        != dict(expected_protected_inventory)
    ):
        raise ValueError("existing AC dev backlog protected inventory changed")


def validate_dev_preimage_only(
    storage_root: Path | str, *, source_identity: Mapping[str, object],
    linked_v3_receipt: Path,
) -> dict[str, object]:
    """Validate an admitted preimage or exact source-owned custody postimage.

    This function is deliberately immutable: it takes no writer lease, creates
    no directory, and opens SQLite only through immutable read-only mode.
    """
    root = _dev_storage_root(
        create=False, isolated_receipt=linked_v3_receipt,
        source_identity=source_identity, allow_postimage=True,
    )
    database = root / AC_DATABASE_DEV_RELATIVE_PATH
    physical = database.stat(follow_symlinks=False)
    pre_sha256 = _durable_database_sha256(database)
    uri = "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        _verify_existing_schema(connection)
        previous_plane = os.environ.pop(RUNTIME_PLANE_ENV, None)
        try:
            _verify_dev_world_schema_inventory(connection)
        finally:
            if previous_plane is not None:
                os.environ[RUNTIME_PLANE_ENV] = previous_plane
        meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
    finally:
        connection.close()
    current = {}
    if meta.get("governance_world_current_process_json"):
        try:
            current = json.loads(str(meta["governance_world_current_process_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("AC dev durable custody projection is unreadable") from exc
        if not isinstance(current, Mapping):
            raise ValueError("AC dev durable custody projection is invalid")
    return {
        "dev_storage_root": str(root), "database_path": str(database),
        "database_identity": {"device": int(physical.st_dev), "inode": int(physical.st_ino)},
        "database_sha256": pre_sha256, "custody_projection": dict(current),
        "has_genesis": bool(meta.get("governance_world_genesis_sha256")),
    }


def commit_dev_child_custody(
    storage_root: Path | str, *, source_identity: Mapping[str, object],
    process_identity: Mapping[str, object], linked_v3_receipt: Path,
    expected_database_identity: Mapping[str, object], expected_pre_sha256: str,
) -> dict[str, object]:
    """Child-owned, CAS-bound custody commit used before any listener bind."""
    pre = validate_dev_preimage_only(
        storage_root, source_identity=source_identity,
        linked_v3_receipt=linked_v3_receipt,
    )
    if (pre["database_identity"] != dict(expected_database_identity)
            or pre["database_sha256"] != expected_pre_sha256):
        raise ValueError("AC dev durable child preimage CAS mismatch")
    # The child has not bound and this is the sole explicitly authorized
    # bootstrap transaction.  Source-contract construction must not inherit
    # the later live server's verify-only capability mode (which would reject
    # its pristine in-memory contract database before comparison).
    previous_plane = os.environ.pop(RUNTIME_PLANE_ENV, None)
    try:
        receipt = bootstrap_dev_governance_store(
            storage_root, source_identity=source_identity,
            process_identity=process_identity, linked_v3_receipt=linked_v3_receipt,
        )
    finally:
        if previous_plane is not None:
            os.environ[RUNTIME_PLANE_ENV] = previous_plane
    database = Path(str(receipt["database_path"]))
    # The writer is closed by bootstrap.  Own durable bytes before readiness.
    checkpoint = sqlite3.connect(str(database), timeout=30)
    try:
        result = tuple(checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if result != (0, 0, 0):
            raise RuntimeError("AC dev durable child checkpoint did not truncate")
    finally:
        checkpoint.close()
    post = validate_dev_preimage_only(
        storage_root, source_identity=source_identity,
        linked_v3_receipt=linked_v3_receipt,
    )
    if dict(post["custody_projection"]) != dict(process_identity):
        raise ValueError("AC dev durable child custody projection mismatch")
    return {
        **receipt, "database_sha256_before": expected_pre_sha256,
        "database_sha256_after": post["database_sha256"],
        "custody_projection": post["custody_projection"],
    }


_COMPLETED_BOOTSTRAP_CUSTODY_KEYS = (
    "governance_world_source_tip_json", "governance_world_source_tip_sha256",
    "governance_world_source_tip_revision", "governance_world_current_process_json",
)


def commit_completed_bootstrap_child_custody(
    storage_root: Path | str, *, expected_database_identity: Mapping[str, object],
    expected_pre_sha256: str, expected_schema_meta: Mapping[str, object],
    custody_updates: Mapping[str, object], expected_managed_inventory: Mapping[str, object],
    expected_protected_inventory: Mapping[str, object],
    expected_protected_projection: Mapping[str, object],
) -> dict[str, object]:
    """CAS exactly four custody cells on a verified dashboard-bootstrap postimage.

    This intentionally has no legacy receipt/adoption fallback.  The caller
    supplies immutable receipt-derived expectations; every other schema/meta
    value is held byte-for-byte stable across the single transaction.
    """
    root = Path(storage_root).expanduser().absolute()
    if root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("AC dev completed bootstrap custody root is invalid")
    database = root / AC_DATABASE_DEV_RELATIVE_PATH
    if (database.is_symlink() or not database.is_file()
            or database.resolve(strict=True) != database):
        raise ValueError("AC dev completed bootstrap custody database path is invalid")
    if (set(custody_updates) != set(_COMPLETED_BOOTSTRAP_CUSTODY_KEYS)
            or not all(isinstance(value, str) for value in custody_updates.values())
            or not isinstance(expected_schema_meta, Mapping)):
        raise ValueError("AC dev completed bootstrap custody context is invalid")
    if (not isinstance(expected_database_identity.get("path"), str)
            or expected_database_identity.get("path") != str(database)
            or not isinstance(expected_database_identity.get("device"), int)
            or not isinstance(expected_database_identity.get("inode"), int)):
        raise ValueError("AC dev completed bootstrap custody receipt identity is invalid")
    physical_stat = database.stat(follow_symlinks=False)
    if physical_stat.st_nlink != 1:
        raise ValueError("AC dev completed bootstrap custody database link count is invalid")
    physical = {"device": int(physical_stat.st_dev), "inode": int(physical_stat.st_ino)}
    claimed_physical = {"device": expected_database_identity["device"],
                        "inode": expected_database_identity["inode"]}
    if claimed_physical != physical:
        raise ValueError("AC dev completed bootstrap custody identity mismatch")
    pre_sha = _durable_database_sha256(database)
    if pre_sha != expected_pre_sha256:
        raise ValueError("AC dev completed bootstrap custody preimage mismatch")
    lease_created = str(database.absolute()) not in _DEV_DATABASE_WRITER_LEASES
    acquire_dev_runtime_writer_lease(root)
    conn = sqlite3.connect(str(database), timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before_meta = {str(key): str(value) for key, value in conn.execute(
            "SELECT key,value FROM schema_meta ORDER BY key"
        )}
        if before_meta != {str(key): str(value) for key, value in expected_schema_meta.items()}:
            raise ValueError("AC dev completed bootstrap custody metadata drift")
        if (backlog_read_schema_managed_inventory(conn) != dict(expected_managed_inventory)
                or backlog_read_schema_protected_inventory(conn) != dict(expected_protected_inventory)
                or _sqlite_logical_projection(
                    conn, exclude_tables=frozenset({"schema_meta"})
                ) != dict(expected_protected_projection)):
            raise ValueError("AC dev completed bootstrap custody schema drift")
        for key in _COMPLETED_BOOTSTRAP_CUSTODY_KEYS:
            conn.execute("UPDATE schema_meta SET value=? WHERE key=?", (custody_updates[key], key))
            if conn.execute("SELECT changes()").fetchone() != (1,):
                raise ValueError("AC dev completed bootstrap custody key is missing")
        after_meta = {str(key): str(value) for key, value in conn.execute(
            "SELECT key,value FROM schema_meta ORDER BY key"
        )}
        changed = {key for key in after_meta if before_meta.get(key) != after_meta.get(key)}
        if (not changed or not changed <= set(_COMPLETED_BOOTSTRAP_CUSTODY_KEYS)
                or "governance_world_current_process_json" not in changed
                or "governance_world_source_tip_revision" not in changed
                or any(after_meta.get(key) != custody_updates[key]
                       for key in _COMPLETED_BOOTSTRAP_CUSTODY_KEYS)):
            raise ValueError("AC dev completed bootstrap custody wrote outside its authority")
        try:
            tip = json.loads(after_meta["governance_world_source_tip_json"])
            revision_before = int(before_meta["governance_world_source_tip_revision"])
            revision_after = int(after_meta["governance_world_source_tip_revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("AC dev completed bootstrap custody source tip is malformed") from exc
        if (not isinstance(tip, Mapping)
                or revision_after != revision_before + 1
                or after_meta["governance_world_source_tip_sha256"]
                != _world_source_tip_hash(dict(tip))):
            raise ValueError("AC dev completed bootstrap custody source tip mismatch")
        if (backlog_read_schema_managed_inventory(conn) != dict(expected_managed_inventory)
                or backlog_read_schema_protected_inventory(conn) != dict(expected_protected_inventory)
                or _sqlite_logical_projection(
                    conn, exclude_tables=frozenset({"schema_meta"})
                ) != dict(expected_protected_projection)):
            raise ValueError("AC dev completed bootstrap custody postcondition failed")
        conn.commit()
    except Exception:
        conn.rollback()
        if lease_created:
            release_dev_runtime_writer_lease(root)
        raise
    finally:
        conn.close()
    checkpoint = sqlite3.connect(str(database), timeout=30)
    try:
        if tuple(checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) != (0, 0, 0):
            raise RuntimeError("AC dev completed bootstrap custody checkpoint failed")
    finally:
        checkpoint.close()
    try:
        _bind_dev_writer_lease_database_identity(database)
    except Exception:
        if lease_created:
            release_dev_runtime_writer_lease(root)
        raise
    post_stat = database.stat(follow_symlinks=False)
    post_identity = {"device": int(post_stat.st_dev), "inode": int(post_stat.st_ino)}
    return {"database_identity": post_identity, "database_sha256_before": pre_sha,
            "database_sha256_after": _durable_database_sha256(database),
            "schema_meta_before": before_meta, "schema_meta_after": after_meta,
            "custody_delta": {key: {"before": before_meta[key], "after": after_meta[key]}
                              for key in _COMPLETED_BOOTSTRAP_CUSTODY_KEYS},
            "changed_custody_keys": sorted(changed)}


def bootstrap_dev_governance_store(
    storage_root: Path | str,
    *,
    source_identity: Mapping[str, object],
    process_identity: Mapping[str, object],
    expected_source_tip_sha256: str = "",
    expected_previous_process_identity: Mapping[str, object] | None = None,
    expected_database_identity: Mapping[str, object] | None = None,
    linked_v3_receipt: Path | None = None,
) -> dict[str, object]:
    """Create or verify the source-only AC dev world without copying stable rows.

    This is the sole schema/genesis bootstrap.  Ordinary dev connections remain
    verify-only, so a later source change cannot silently migrate a running
    world.  A restart may re-read an exact existing genesis but cannot replace
    it or import any stable governance bytes.
    """

    root_input = Path(storage_root).expanduser().absolute()
    # Bootstrap is an ingress too: reject a caller-selected world before it
    # can create a directory or initialize SQLite.
    env_raw = os.environ.get(AC_DEV_STORAGE_ROOT_ENV, "").strip()
    if not env_raw or Path(env_raw).expanduser().absolute() != root_input:
        raise ValueError("AC dev bootstrap storage root env mismatch")
    root_existed = root_input.exists()
    shared_raw = os.environ.get("SHARED_VOLUME_PATH", "").strip()
    if shared_raw:
        shared = Path(shared_raw).expanduser().absolute()
        if root_input == shared or root_input in shared.parents or shared in root_input.parents:
            raise ValueError("AC dev storage root must be disjoint from shared storage")
    root = _dev_storage_root(
        create=not root_existed, isolated_receipt=linked_v3_receipt,
        source_identity=source_identity, allow_postimage=True,
    )
    source = {
        "root": str(source_identity.get("root") or "").strip(),
        "branch": str(source_identity.get("branch") or "").strip(),
        "commit": str(source_identity.get("commit") or "").strip().lower(),
        "source_sha256": str(source_identity.get("source_sha256") or "")
        .strip()
        .lower(),
    }
    process = {
        "pid": int(process_identity.get("pid") or 0),
        "start_identity": str(process_identity.get("start_identity") or "").strip(),
    }
    for key in (
        "argv", "cwd", "source_root", "source_commit", "source_tree",
        "dev_storage_root", "project_id", "port", "policy", "launch_id",
    ):
        if key in process_identity:
            process[key] = process_identity[key]
    if not (
        source["root"]
        and source["branch"] == "codex/ac-dev"
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["commit"])
        and re.fullmatch(r"sha256:[0-9a-f]{64}", source["source_sha256"])
        and process["pid"] > 0
        and process["start_identity"]
    ):
        raise ValueError("AC dev source/process bootstrap identity is incomplete")
    database = root / AC_DATABASE_DEV_RELATIVE_PATH
    created = not database.exists()
    # A root that pre-existed without its DB is neither a fresh bootstrap nor a
    # proven prior world.  Do not create child directories in that case.
    if root_existed and created:
        raise ValueError("AC dev fresh bootstrap requires an absent dedicated root")
    if not created and linked_v3_receipt is None:
        _validate_existing_adoption_receipt(root)
    else:
        database.parent.mkdir(parents=True, exist_ok=True)
    if database.parent.is_symlink() or database.parent.resolve(strict=True) != database.parent:
        raise ValueError("AC dev governance directory cannot be a symlink")
    companions = [
        candidate for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(str(database) + suffix)).exists()
    ]
    # Only a verified existing world can recover SQLite's normal WAL/SHM
    # artifacts.  A fresh ingress remains hostile to every preloaded byte.
    if created and companions:
        raise ValueError("AC dev bootstrap rejects reused WAL/SHM/journal state")
    if not created:
        _sqlite_adoption_identity(database, required=True)
    for companion in companions:
        _sqlite_adoption_identity(companion, required=True)
    genesis = {
        "schema_version": AC_WORLD_GENESIS_SCHEMA,
        "world_id": AC_DEV_WORLD_ID,
        "project_id": AC_PROJECT_ID,
        "source_identity": source,
        "bootstrap_process_identity": process,
        "source_only": True,
        "rows_copied": 0,
    }
    genesis_sha256 = _world_genesis_hash(genesis)
    source_tip_identity = dict(source)
    source_tip_sha256 = _world_source_tip_hash(source_tip_identity)
    source_tip_revision = 1
    current_process_identity = dict(process)
    source_upgraded = False
    if database.is_symlink():
        raise ValueError("AC dev governance database cannot be a symlink")
    cow_successor_receipt: dict[str, object] | None = None
    if not created:
        # Resolve the exceptional physical successor before opening the writer;
        # receipt validation includes the no-holder/no-sidecar admission gate.
        probe = sqlite3.connect(
            "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1",
            uri=True,
        )
        try:
            probe_meta = dict(probe.execute(
                "SELECT key,value FROM schema_meta WHERE key='governance_world_genesis_json'"
            ))
        except sqlite3.Error as exc:
            raise ValueError("existing AC dev world genesis is unreadable") from exc
        finally:
            probe.close()
        try:
            probe_genesis = json.loads(str(probe_meta.get("governance_world_genesis_json") or ""))
        except ValueError:
            probe_genesis = {}
        probe_stored = dict(probe_genesis.get("database_identity") or {}) if isinstance(probe_genesis, Mapping) else {}
        probe_stat = database.stat(follow_symlinks=False)
        if (probe_stored.get("device"), probe_stored.get("inode")) != (
                int(probe_stat.st_dev), int(probe_stat.st_ino)):
            cow_successor_receipt = validate_dev_cow_successor_receipt(root)
    lease_created = str(database.absolute()) not in _DEV_DATABASE_WRITER_LEASES
    acquire_dev_runtime_writer_lease(root)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(database), timeout=30)
        conn.row_factory = sqlite3.Row
        admitted_preimage = False
        if not created and linked_v3_receipt is not None:
            existing_meta = dict(conn.execute(
                "SELECT key, value FROM schema_meta WHERE key IN "
                "('governance_world_genesis_sha256','governance_world_genesis_json')"
            ))
            admitted_preimage = not bool(existing_meta.get("governance_world_genesis_sha256"))
        if created or admitted_preimage:
            _configure_connection(conn, busy_timeout=10000)
            if created:
                _ensure_schema(conn)
            _verify_dev_world_schema_inventory(conn)
            required_tables, _allowed_tables, _source_objects = _source_schema_table_contract()
            for table in sorted(
                name
                for name in required_tables
                if name not in {"schema_meta", "sqlite_sequence"}
                and not name.startswith("memories_fts")
            ):
                if int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]):
                    raise ValueError("AC dev fresh bootstrap found preloaded business rows")
            physical = database.stat(follow_symlinks=False)
            root_physical = root.stat(follow_symlinks=False)
            genesis["storage_root_identity"] = {
                "path": str(root),
                "device": int(root_physical.st_dev),
                "inode": int(root_physical.st_ino),
            }
            genesis["database_identity"] = {
                "device": int(physical.st_dev),
                "inode": int(physical.st_ino),
                "relative_path_sha256": "sha256:"
                + hashlib.sha256(AC_DATABASE_DEV_RELATIVE_PATH.encode("utf-8")).hexdigest(),
            }
            genesis["source_schema_sha256"] = _source_schema_inventory_hash(
                conn, required_tables
            )
            genesis_sha256 = _world_genesis_hash(genesis)
            conn.executemany(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                (
                    ("governance_world_id", AC_DEV_WORLD_ID),
                    ("governance_world_genesis_sha256", genesis_sha256),
                    (
                        "governance_world_genesis_json",
                        json.dumps(genesis, sort_keys=True, separators=(",", ":")),
                    ),
                    (
                        "governance_world_source_tip_json",
                        json.dumps(source_tip_identity, sort_keys=True, separators=(",", ":")),
                    ),
                    ("governance_world_source_tip_sha256", source_tip_sha256),
                    ("governance_world_source_tip_revision", "1"),
                    (
                        "governance_world_current_process_json",
                        json.dumps(current_process_identity, sort_keys=True, separators=(",", ":")),
                    ),
                ),
            )
            conn.commit()
        else:
            _verify_existing_schema(conn)
            _verify_dev_world_schema_inventory(conn)
            if cow_successor_receipt is not None:
                _verify_current_dev_backlog_runtime_invariants(
                    conn,
                    expected_protected_inventory=dict(
                        dict(cow_successor_receipt.get("successor") or {}).get(
                            "protected_inventory"
                        ) or {}
                    ),
                )
            meta = dict(conn.execute("SELECT key, value FROM schema_meta"))
            try:
                stored_genesis = json.loads(
                    str(meta.get("governance_world_genesis_json") or "")
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("existing AC dev world genesis is unreadable") from exc
            stored_hash = str(meta.get("governance_world_genesis_sha256") or "")
            if not (
                meta.get("governance_world_id") == AC_DEV_WORLD_ID
                and isinstance(stored_genesis, Mapping)
                and stored_genesis.get("schema_version") == AC_WORLD_GENESIS_SCHEMA
                and stored_genesis.get("world_id") == AC_DEV_WORLD_ID
                and stored_genesis.get("project_id") == AC_PROJECT_ID
                and stored_genesis.get("source_only") is True
                and stored_genesis.get("rows_copied") == 0
                and stored_hash == _world_genesis_hash(stored_genesis)
            ):
                raise ValueError("existing AC dev world genesis is invalid")
            stored_database = dict(stored_genesis.get("database_identity") or {})
            stored_root = dict(stored_genesis.get("storage_root_identity") or {})
            current_root = root.stat(follow_symlinks=False)
            current_database = database.stat(follow_symlinks=False)
            if (
                (stored_database.get("device") != int(current_database.st_dev)
                 or stored_database.get("inode") != int(current_database.st_ino))
                or stored_root.get("path") != str(root)
                or stored_root.get("device") != int(current_root.st_dev)
                or stored_root.get("inode") != int(current_root.st_ino)
            ):
                # A content-addressed COW successor is the only authority that
                # may replace the physical genesis inode.  Genesis bytes and
                # every logical/source-schema check below remain authoritative.
                if (stored_root.get("path") != str(root)
                        or stored_root.get("device") != int(current_root.st_dev)
                        or stored_root.get("inode") != int(current_root.st_ino)):
                    raise ValueError("existing AC dev world storage/database identity changed")
                if cow_successor_receipt is None:
                    raise ValueError("existing AC dev world COW successor receipt is missing")
                predecessor = dict(dict(cow_successor_receipt.get("predecessor") or {}).get("backup") or {})
                successor_identity = dict(dict(cow_successor_receipt.get("successor") or {}).get("identity") or {})
                if (stored_database.get("device") != predecessor.get("device")
                        or stored_database.get("inode") != predecessor.get("inode")
                        or successor_identity.get("device") != int(current_database.st_dev)
                        or successor_identity.get("inode") != int(current_database.st_ino)):
                    raise ValueError("existing AC dev world COW successor identity mismatch")
            required_tables, _allowed_tables, _source_objects = _source_schema_table_contract()
            if (cow_successor_receipt is None
                    and stored_genesis.get("source_schema_sha256")
                    != _source_schema_inventory_hash(conn, required_tables)):
                raise ValueError("existing AC dev source schema contract changed")
            genesis = dict(stored_genesis)
            genesis_sha256 = stored_hash
            try:
                source_tip_identity = json.loads(
                    str(meta.get("governance_world_source_tip_json") or "")
                )
            except (TypeError, ValueError):
                source_tip_identity = dict(stored_genesis.get("source_identity") or {})
            if not isinstance(source_tip_identity, Mapping):
                raise ValueError("existing AC dev source tip is unreadable")
            source_tip_identity = dict(source_tip_identity)
            source_tip_sha256 = str(
                meta.get("governance_world_source_tip_sha256")
                or _world_source_tip_hash(source_tip_identity)
            )
            if source_tip_sha256 != _world_source_tip_hash(source_tip_identity):
                raise ValueError("existing AC dev source tip hash is invalid")
            source_tip_revision = int(
                meta.get("governance_world_source_tip_revision") or 1
            )
            try:
                current_process_identity = json.loads(
                    str(meta.get("governance_world_current_process_json") or "")
                )
            except (TypeError, ValueError):
                current_process_identity = dict(
                    stored_genesis.get("bootstrap_process_identity") or {}
                )
            if not isinstance(current_process_identity, Mapping):
                raise ValueError("existing AC dev process identity is unreadable")
            current_process_identity = dict(current_process_identity)

            metadata = database.stat(follow_symlinks=False)
            actual_database_identity = {
                "schema_version": "ac_governance_database_identity.v2",
                "world_id": AC_DEV_WORLD_ID,
                "project_id": AC_PROJECT_ID,
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
                "relative_path_sha256": "sha256:"
                + hashlib.sha256(
                    AC_DATABASE_DEV_RELATIVE_PATH.encode("utf-8")
                ).hexdigest(),
                "genesis_sha256": genesis_sha256,
            }
            if expected_database_identity is not None and (
                dict(expected_database_identity) != actual_database_identity
            ):
                raise ValueError("AC dev source upgrade database identity mismatch")

            if source != source_tip_identity:
                if (
                    expected_source_tip_sha256
                    and expected_source_tip_sha256 != source_tip_sha256
                ):
                    raise ValueError("AC dev source tip CAS mismatch")
                if expected_previous_process_identity is not None and (
                    dict(expected_previous_process_identity)
                    != current_process_identity
                ):
                    raise ValueError("AC dev source upgrade process identity mismatch")
            # Receipt, stable binding, schema/genesis, physical identity and
            # exact clean source lineage are now all verified before SQLite
            # may touch WAL.  The same check is intentional on a no-op restart.
            _verify_dev_source_upgrade(source_tip_identity, source)
            _recover_verified_existing_dev_sqlite(root, database)
            if source != source_tip_identity:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    locked_meta = dict(
                        conn.execute(
                            "SELECT key, value FROM schema_meta WHERE key IN "
                            "('governance_world_source_tip_sha256', "
                            "'governance_world_source_tip_revision')"
                        )
                    )
                    locked_hash = str(
                        locked_meta.get("governance_world_source_tip_sha256")
                        or _world_source_tip_hash(source_tip_identity)
                    )
                    locked_revision = int(
                        locked_meta.get("governance_world_source_tip_revision")
                        or source_tip_revision
                    )
                    if (
                        locked_hash != source_tip_sha256
                        or locked_revision != source_tip_revision
                    ):
                        raise ValueError("AC dev source tip CAS changed while locked")
                    next_hash = _world_source_tip_hash(source)
                    conn.executemany(
                        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                        (
                            (
                                "governance_world_source_tip_json",
                                json.dumps(source, sort_keys=True, separators=(",", ":")),
                            ),
                            ("governance_world_source_tip_sha256", next_hash),
                            (
                                "governance_world_source_tip_revision",
                                str(source_tip_revision + 1),
                            ),
                            (
                                "governance_world_current_process_json",
                                json.dumps(process, sort_keys=True, separators=(",", ":")),
                            ),
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                source_tip_identity = dict(source)
                source_tip_sha256 = _world_source_tip_hash(source_tip_identity)
                source_tip_revision += 1
                current_process_identity = dict(process)
                source_upgraded = True
            elif process != current_process_identity:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    locked_value = conn.execute(
                        "SELECT value FROM schema_meta WHERE key = "
                        "'governance_world_current_process_json'"
                    ).fetchone()
                    locked_process = json.loads(str(locked_value[0] or "{}")) if locked_value else {}
                    if locked_process != current_process_identity:
                        raise ValueError("AC dev current process custody changed while locked")
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                        ("governance_world_current_process_json",
                         json.dumps(process, sort_keys=True, separators=(",", ":"))),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                current_process_identity = dict(process)
    except Exception:
        if conn is not None:
            conn.close()
        if created:
            for suffix in ("", "-wal", "-shm", "-journal"):
                candidate = Path(str(database) + suffix)
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
        if lease_created:
            release_dev_runtime_writer_lease(root)
        raise
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    _bind_dev_writer_lease_database_identity(database)
    metadata = database.stat(follow_symlinks=False)
    identity = {
        "schema_version": "ac_governance_database_identity.v2",
        "world_id": AC_DEV_WORLD_ID,
        "project_id": AC_PROJECT_ID,
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "relative_path_sha256": "sha256:"
        + hashlib.sha256(AC_DATABASE_DEV_RELATIVE_PATH.encode("utf-8")).hexdigest(),
        "genesis_sha256": genesis_sha256,
    }
    return {
        **genesis,
        "genesis_sha256": genesis_sha256,
        "database_path": str(database),
        "database_identity": identity,
        "created": created,
        "restart_safe": True,
        "legacy_rows_imported": False,
        "current_process_identity": current_process_identity,
        "source_tip_identity": source_tip_identity,
        "source_tip_sha256": source_tip_sha256,
        "source_tip_revision": source_tip_revision,
        "source_upgraded": source_upgraded,
    }


def _cutover_hash(payload: Mapping[str, object]) -> str:
    authority = dict(payload)
    legacy = authority.get("legacy_database_identity")
    if isinstance(legacy, Mapping):
        # The stable database is still live while clients drain.  Its size,
        # ownership timestamps, and content digest are bounded observations,
        # not cross-phase activation authority.  Only the canonical physical
        # file identity is stable across preflight and activation.
        authority["legacy_database_identity"] = {
            key: legacy[key]
            for key in ("path", "device", "inode")
            if key in legacy
        }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            authority,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _atomic_cutover_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("AC dev cutover directory cannot be a symlink")
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        body = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_cutover_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("AC dev cutover checkpoint is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("AC dev cutover checkpoint is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("AC dev cutover checkpoint is invalid")
    return dict(value)


def _default_cutover_listener_probe(port: int) -> dict[str, object]:
    """Inspect one local listener without starting, stopping, or signalling it."""

    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fpn"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    pids = sorted(
        {
            int(line[1:])
            for line in result.stdout.splitlines()
            if line.startswith("p") and line[1:].isdigit()
        }
    )
    if len(pids) > 1:
        raise ValueError("AC dev cutover listener ownership is ambiguous")
    pid = pids[0] if pids else 0
    start_identity = ""
    if pid:
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        start_identity = process.stdout.strip() if process.returncode == 0 else ""
    return {
        "port": int(port),
        "listening": bool(pid),
        "pid": pid,
        "process_start_identity": start_identity,
        "listener_addresses": sorted(
            {
                line[1:].removesuffix(" (LISTEN)")
                for line in result.stdout.splitlines()
                if line.startswith("n") and line[1:].strip()
            }
        ),
        "source_commit": (
            os.environ.get("AMING_CLAW_STABLE_ANCHOR_COMMIT", "").strip().lower()
            if int(port) == 40000
            else ""
        ),
    }


def _require_ac_dev_graph_materialization_runtime_custody(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Prove this process owns the exact live dev listener and DB lease."""

    opened = _connection_main_database_identity(conn)
    if opened is None:
        raise ValueError("AC dev graph materialization database identity is unavailable")
    database, metadata = opened
    expected_database = _dev_database_path()
    if (
        database != expected_database
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (
            int(expected_database.stat(follow_symlinks=False).st_dev),
            int(expected_database.stat(follow_symlinks=False).st_ino),
        )
    ):
        raise ValueError("AC dev graph materialization database custody mismatch")

    server_source = Path(__file__).with_name("server.py")
    source_sha256 = "sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest()
    launch = validate_dev_launch_receipt(
        _dev_storage_root(create=False), source_sha256=source_sha256
    )
    if (
        launch.get("runtime_plane") != DEV_RUNTIME_PLANE
        or launch.get("world_id", AC_DEV_WORLD_ID) != AC_DEV_WORLD_ID
        or launch.get("project_id") != AC_PROJECT_ID
        or launch.get("port") != 40008
        or launch.get("background", False) is not False
    ):
        raise ValueError("AC dev graph materialization launch custody mismatch")

    listener = _default_cutover_listener_probe(40008)
    _validate_ac_dev_graph_materialization_listener(listener)

    with _DEV_DATABASE_WRITER_LEASES_LOCK:
        lease = _DEV_DATABASE_WRITER_LEASES.get(str(expected_database))
        if (
            not lease
            or getattr(lease.get("handle"), "closed", True)
            or lease.get("owner_pid") != os.getpid()
            or lease.get("owner_start_identity") != _writer_process_start_identity()
            or lease.get("database_device") != int(metadata.st_dev)
            or lease.get("database_inode") != int(metadata.st_ino)
        ):
            raise ValueError("AC dev graph materialization writer custody mismatch")
    return {
        "schema_version": "ac_dev_graph_materialization_runtime_custody.v1",
        "runtime_plane": DEV_RUNTIME_PLANE,
        "world_id": AC_DEV_WORLD_ID,
        "project_id": AC_PROJECT_ID,
        "host": "127.0.0.1",
        "port": 40008,
        "pid": os.getpid(),
        "database_device": int(metadata.st_dev),
        "database_inode": int(metadata.st_ino),
    }


def _validate_ac_dev_graph_materialization_listener(
    listener: Mapping[str, object],
) -> None:
    """Validate an OS-observed exact loopback listener owned by this process."""

    if (
        listener.get("port") != 40008
        or listener.get("listening") is not True
        or int(listener.get("pid") or 0) != os.getpid()
        or listener.get("listener_addresses") != ["127.0.0.1:40008"]
    ):
        raise ValueError("AC dev graph materialization listener custody mismatch")
    process_started = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    if (
        process_started.returncode != 0
        or not process_started.stdout.strip()
        or str(listener.get("process_start_identity") or "").strip()
        != process_started.stdout.strip()
    ):
        raise ValueError("AC dev graph materialization listener process mismatch")


def _fd_sparse_content_digest(descriptor: int, *, size: int) -> str:
    """Hash one already-open archive descriptor, including extent positions."""

    digest = hashlib.sha256()
    digest.update(b"ac-cutover-sparse-v1\0")
    digest.update(str(int(size)).encode("ascii"))
    offset = 0
    while offset < size:
        try:
            data_offset = os.lseek(descriptor, offset, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break
            if exc.errno in {errno.EINVAL, errno.ENOTSUP}:
                data_offset = 0
            else:
                raise
        try:
            hole_offset = os.lseek(descriptor, data_offset, os.SEEK_HOLE)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
            hole_offset = size
        extent_length = min(hole_offset, size) - data_offset
        digest.update(f"{data_offset}:{extent_length}:".encode("ascii"))
        os.lseek(descriptor, data_offset, os.SEEK_SET)
        remaining = extent_length
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("legacy AC archive changed during digest")
            digest.update(chunk)
            remaining -= len(chunk)
        offset = max(hole_offset, data_offset + 1)
        if data_offset == 0 and hole_offset == size:
            break
    return "sha256-sparse-v1:" + digest.hexdigest()


def _cutover_database_stat(
    path: Path,
    *,
    expected_size: int | None = None,
    content_digest: bool = False,
    cached_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    absolute = path.expanduser().absolute()
    if absolute.is_symlink():
        raise ValueError("AC dev cutover database cannot be a symlink")
    descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        path_before = os.stat(absolute, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or absolute.resolve(strict=True) != absolute
            or (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino)
        ):
            raise ValueError("AC dev cutover database identity is invalid")
        if expected_size is not None and int(before.st_size) != int(expected_size):
            raise ValueError("legacy AC archive size identity mismatch")
        identity = {
            "path": str(absolute),
            "device": int(before.st_dev),
            "inode": int(before.st_ino),
            "size": int(before.st_size),
            "uid": int(before.st_uid),
            "mtime_ns": int(before.st_mtime_ns),
            "ctime_ns": int(before.st_ctime_ns),
        }
        if content_digest:
            cached = dict(cached_identity or {})
            authority_keys = ("path", "device", "inode")
            if cached and any(
                cached.get(key) != identity[key] for key in authority_keys
            ):
                raise ValueError("legacy AC archive identity changed")
            actual_digest = _fd_sparse_content_digest(
                descriptor, size=int(before.st_size)
            )
            identity["content_digest"] = actual_digest
        after = os.fstat(descriptor)
        path_after = os.stat(absolute, follow_symlinks=False)
        immutable_keys = (
            "st_dev", "st_ino", "st_mode", "st_size", "st_uid", "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(before, key) != getattr(after, key) for key in immutable_keys) or (
            after.st_dev,
            after.st_ino,
        ) != (path_after.st_dev, path_after.st_ino):
            raise ValueError("legacy AC archive changed during content proof")
        return identity
    finally:
        os.close(descriptor)


def _inspect_dev_world_cutover(
    *,
    legacy_database_path: Path | str,
    storage_root: Path | str,
    source_identity: Mapping[str, object],
    process_identity: Mapping[str, object],
    expected_dev_database_identity: Mapping[str, object],
    listener_probe,
    expected_legacy_database_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    root = _absolute_non_symlink_root(
        Path(storage_root).expanduser().absolute(), create=False
    )
    source = {
        "root": str(source_identity.get("root") or "").strip(),
        "branch": str(source_identity.get("branch") or "").strip(),
        "commit": str(source_identity.get("commit") or "").strip().lower(),
        "source_sha256": str(source_identity.get("source_sha256") or "").strip().lower(),
    }
    process = {
        "pid": int(process_identity.get("pid") or 0),
        "start_identity": str(process_identity.get("start_identity") or "").strip(),
    }
    if process["pid"] <= 0 or not process["start_identity"]:
        raise ValueError("AC dev cutover operator process identity is incomplete")
    _verify_dev_source_upgrade(source, source)

    legacy = _cutover_database_stat(
        Path(legacy_database_path),
        content_digest=True,
        cached_identity=expected_legacy_database_identity,
    )
    new_database = root / AC_DATABASE_DEV_RELATIVE_PATH
    new_stat = _cutover_database_stat(new_database)
    if (legacy["device"], legacy["inode"]) == (
        new_stat["device"],
        new_stat["inode"],
    ):
        raise ValueError("AC dev cutover old/new database identity overlaps")

    for database in (Path(legacy["path"]), Path(new_stat["path"])):
        companions = [
            str(candidate)
            for suffix in ("-wal", "-shm", "-journal")
            if (candidate := Path(str(database) + suffix)).exists()
        ]
        if companions:
            raise ValueError("AC dev cutover WAL/SHM ownership must be absent")

    with closing(sqlite3.connect(new_database)) as connection:
        previous_plane = os.environ.get(RUNTIME_PLANE_ENV)
        previous_root = os.environ.get(AC_DEV_STORAGE_ROOT_ENV)
        os.environ[RUNTIME_PLANE_ENV] = DEV_RUNTIME_PLANE
        os.environ[AC_DEV_STORAGE_ROOT_ENV] = str(root)
        try:
            new_identity = canonical_ac_database_identity(connection)
        finally:
            if previous_plane is None:
                os.environ.pop(RUNTIME_PLANE_ENV, None)
            else:
                os.environ[RUNTIME_PLANE_ENV] = previous_plane
            if previous_root is None:
                os.environ.pop(AC_DEV_STORAGE_ROOT_ENV, None)
            else:
                os.environ[AC_DEV_STORAGE_ROOT_ENV] = previous_root
    if new_identity != dict(expected_dev_database_identity):
        raise ValueError("AC dev cutover new database identity mismatch")

    stable_listener = dict(listener_probe(40000) or {})
    dev_listener = dict(listener_probe(40008) or {})
    if not (
        stable_listener.get("port") == 40000
        and stable_listener.get("listening") is True
        and int(stable_listener.get("pid") or 0) > 0
        and str(stable_listener.get("process_start_identity") or "").strip()
        and re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}",
            str(stable_listener.get("source_commit") or "").strip().lower(),
        )
    ):
        raise ValueError("AC dev cutover stable listener identity is invalid")
    if not (
        dev_listener.get("port") == 40008
        and dev_listener.get("listening") is False
        and int(dev_listener.get("pid") or 0) == 0
    ):
        raise ValueError("AC dev cutover requires port 40008 inactive")

    lease = None
    with _DEV_DATABASE_WRITER_LEASES_LOCK:
        writer = _DEV_DATABASE_WRITER_LEASES.get(str(new_database.resolve(strict=True)))
        if writer:
            if (
                int(writer.get("owner_pid") or 0) != os.getpid()
                or writer.get("owner_start_identity") != _writer_process_start_identity()
                or (writer.get("database_device"), writer.get("database_inode"))
                != (int(new_stat["device"]), int(new_stat["inode"]))
            ):
                raise RuntimeError("AC dev cutover found a foreign in-process writer")
        else:
            lease = _exclusive_writer_file_lease(new_database)
    try:
        return {
            "schema_version": AC_DEV_CUTOVER_SCHEMA,
            "legacy_database_identity": legacy,
            "new_database_stat": new_stat,
            "new_database_identity": new_identity,
            "source_identity": source,
            "operator_process_identity": process,
            "stable_listener_identity": stable_listener,
            "dev_listener_identity": dev_listener,
            "wal_shm_absent": True,
            "concurrent_writer_absent": True,
            "legacy_rows_copied": 0,
            "facts_copied": 0,
        }
    finally:
        if lease is not None:
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
            lease.close()


def preflight_dev_world_cutover(
    *,
    legacy_database_path: Path | str,
    storage_root: Path | str,
    source_identity: Mapping[str, object],
    process_identity: Mapping[str, object],
    expected_dev_database_identity: Mapping[str, object],
    listener_probe=None,
) -> dict[str, object]:
    """Write one restart-safe checkpoint after an entirely bounded preflight."""

    probe = listener_probe or _default_cutover_listener_probe
    core = _inspect_dev_world_cutover(
        legacy_database_path=legacy_database_path,
        storage_root=storage_root,
        source_identity=source_identity,
        process_identity=process_identity,
        expected_dev_database_identity=expected_dev_database_identity,
        listener_probe=probe,
    )
    preflight_hash = _cutover_hash(core)
    root = Path(storage_root).expanduser().absolute()
    checkpoint = root / "cutover" / "checkpoints" / (
        preflight_hash.removeprefix("sha256:") + ".json"
    )
    payload = {
        **core,
        "status": "ready",
        "preflight_hash": preflight_hash,
        "checkpoint_path": str(checkpoint),
    }
    if checkpoint.exists():
        existing = _read_cutover_json(checkpoint)
        existing_core = {
            key: value
            for key, value in existing.items()
            if key not in {"status", "preflight_hash", "checkpoint_path"}
        }
        if (
            existing.get("status") != "ready"
            or existing.get("preflight_hash") != preflight_hash
            or existing.get("checkpoint_path") != str(checkpoint)
            or _cutover_hash(existing_core) != preflight_hash
        ):
            raise ValueError("AC dev cutover checkpoint collision")
        return existing
    else:
        _atomic_cutover_json(checkpoint, payload)
    return payload


def _load_cutover_preflight(storage_root: Path | str, preflight_hash: str) -> dict[str, object]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(preflight_hash or "")):
        raise ValueError("AC dev cutover preflight hash is invalid")
    root = _absolute_non_symlink_root(
        Path(storage_root).expanduser().absolute(), create=False
    )
    checkpoint = root / "cutover" / "checkpoints" / (
        str(preflight_hash).removeprefix("sha256:") + ".json"
    )
    payload = _read_cutover_json(checkpoint)
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"status", "preflight_hash", "checkpoint_path"}
    }
    if (
        payload.get("status") != "ready"
        or payload.get("checkpoint_path") != str(checkpoint)
        or payload.get("preflight_hash") != preflight_hash
        or _cutover_hash(core) != preflight_hash
    ):
        raise ValueError("AC dev cutover checkpoint hash mismatch")
    return payload


def activate_dev_world_cutover(
    *,
    storage_root: Path | str,
    preflight_hash: str,
    listener_probe=None,
) -> dict[str, object]:
    """Atomically activate one unchanged preflight; never starts a process."""

    checkpoint = _load_cutover_preflight(storage_root, preflight_hash)
    probe = listener_probe or _default_cutover_listener_probe
    current = _inspect_dev_world_cutover(
        legacy_database_path=checkpoint["legacy_database_identity"]["path"],
        storage_root=storage_root,
        source_identity=checkpoint["source_identity"],
        process_identity=checkpoint["operator_process_identity"],
        expected_dev_database_identity=checkpoint["new_database_identity"],
        listener_probe=probe,
        expected_legacy_database_identity=checkpoint["legacy_database_identity"],
    )
    if _cutover_hash(current) != preflight_hash:
        raise ValueError("AC dev cutover preflight changed before activation")
    root = Path(storage_root).expanduser().absolute()
    active_path = root / "cutover" / "active.json"
    active = {
        "schema_version": AC_DEV_CUTOVER_SCHEMA,
        "status": "active",
        "preflight_hash": preflight_hash,
        "checkpoint_path": checkpoint["checkpoint_path"],
        "new_database_identity": checkpoint["new_database_identity"],
        "source_identity": checkpoint["source_identity"],
        "legacy_database_identity": checkpoint["legacy_database_identity"],
        "legacy_rows_copied": 0,
        "facts_copied": 0,
    }
    if active_path.exists():
        if _read_cutover_json(active_path) != active:
            raise ValueError("a different AC dev cutover is already active")
        return {**active, "idempotent": True}
    _atomic_cutover_json(active_path, active)
    return {**active, "idempotent": False}


def validate_dev_world_cutover_activation(
    *,
    storage_root: Path | str,
    expected_dev_database_identity: Mapping[str, object],
    source_identity: Mapping[str, object],
    listener_probe=None,
) -> dict[str, object]:
    """Validate the atomic activation and a same-or-descendant source tip."""

    root = _absolute_non_symlink_root(
        Path(storage_root).expanduser().absolute(), create=False
    )
    try:
        active = _read_cutover_json(root / "cutover" / "active.json")
    except ValueError as exc:
        raise ValueError("AC dev cutover activation is unavailable") from exc
    if active.get("status") != "active":
        raise ValueError("AC dev cutover activation is not active")
    checkpoint = _load_cutover_preflight(
        root, str(active.get("preflight_hash") or "")
    )
    if (
        active.get("new_database_identity")
        != dict(expected_dev_database_identity)
        or checkpoint.get("new_database_identity")
        != dict(expected_dev_database_identity)
    ):
        raise ValueError("AC dev cutover activation database identity mismatch")
    activated_source = dict(active.get("source_identity") or {})
    current_source = dict(source_identity)
    if current_source != activated_source:
        _verify_dev_source_upgrade(activated_source, current_source)
    return {
        **active,
        "active": True,
        "current_source_identity": current_source,
        "source_descendant_verified": True,
    }


def rollback_dev_world_cutover(
    *,
    storage_root: Path | str,
    preflight_hash: str,
) -> dict[str, object]:
    """Atomically remove only the matching new-world activation marker."""

    root = _absolute_non_symlink_root(
        Path(storage_root).expanduser().absolute(), create=False
    )
    active_path = root / "cutover" / "active.json"
    active = _read_cutover_json(active_path)
    if active.get("preflight_hash") != preflight_hash:
        raise ValueError("AC dev cutover rollback hash mismatch")
    destination = root / "cutover" / "rolled-back" / (
        preflight_hash.removeprefix("sha256:") + ".json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("AC dev cutover rollback destination already exists")
    os.replace(active_path, destination)
    directory_fd = os.open(active_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {
        "schema_version": AC_DEV_CUTOVER_SCHEMA,
        "status": "rolled_back",
        "preflight_hash": preflight_hash,
        "active": False,
        "legacy_unchanged": True,
        "new_process_started": False,
    }


def _governance_root() -> Path:
    """Root directory for governance data."""
    if _is_dev_runtime():
        root = _dev_storage_root(create=False) / "governance"
        if not root.is_dir():
            raise FileNotFoundError(
                "AC dev runtime governance root must already exist: " + str(root)
            )
        return root
    return Path(tasks_root()) / "state" / "governance"


def _normalize_id(pid: str) -> str:
    """Normalize project ID inline (avoid circular import with project_service)."""
    import re
    s = pid.strip()
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


def validate_project_id_syntax(project_id: str, *, require_exact: bool = False) -> str:
    """Validate a project identifier without consulting or changing storage.

    ``require_exact`` is used by the dev-plane external discovery boundary.  It
    deliberately rejects aliases (including camelCase and underscores) so one
    registered database has exactly one request identity.
    """

    raw = str(project_id or "").strip()
    if (
        not raw
        or raw in {".", ".."}
        or "/" in raw
        or "\\" in raw
        or "\x00" in raw
        or any(part in {".", ".."} for part in raw.replace("\\", "/").split("/"))
    ):
        raise ValueError("invalid governance project_id")
    normalized = _normalize_id(raw)
    if not normalized:
        raise ValueError("invalid governance project_id")
    if require_exact and (
        raw != normalized
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized)
    ):
        raise ValueError("governance project_id must use its exact normalized key")
    return normalized


def _is_dev_runtime() -> bool:
    return os.environ.get(RUNTIME_PLANE_ENV, "").strip().lower() == DEV_RUNTIME_PLANE


def assert_runtime_world_project_identity(
    project_id: str,
    *,
    referenced_project_ids: Mapping[str, Any] | None = None,
) -> str:
    """Validate one write scope against the source-backed runtime world.

    This is deliberately a validation helper, not another authority engine.
    ContractRuntime remains the authority for line/state/Facts; this function
    only prevents an internal writer from persisting a row into the wrong
    physical world when it bypasses the HTTP transport guard.

    Generic test/library use retains the historical normalized project key.
    A launched service always sets an explicit ``stable`` or ``dev`` plane:
    stable rejects AC (including aliases), while dev accepts the exact
    canonical spelling only.  Any supplied secondary project identity must
    name the same world-owned project.
    """

    raw = str(project_id or "").strip()
    canonical = validate_project_id_syntax(raw)
    plane = os.environ.get(RUNTIME_PLANE_ENV, "").strip().lower()
    if plane == DEV_RUNTIME_PLANE:
        if raw != AC_PROJECT_ID or canonical != AC_PROJECT_ID:
            raise ValueError(
                "AC dev world accepts exact project_id=" + AC_PROJECT_ID
            )
    elif plane in {"stable", "generic"} and canonical == AC_PROJECT_ID:
        raise ValueError(f"{plane} world cannot persist AC project state")

    if plane in {DEV_RUNTIME_PLANE, "stable", "generic"}:
        for label, value in dict(referenced_project_ids or {}).items():
            if value in (None, ""):
                continue
            ref_raw = str(value).strip()
            ref_canonical = validate_project_id_syntax(ref_raw)
            if ref_raw != raw or ref_canonical != canonical:
                raise ValueError(
                    f"{label} crosses runtime project/world boundary: "
                    f"expected exact {raw}"
                )
    return canonical


def validate_project_id(project_id: str) -> str:
    """Validate one project id before any filesystem or SQLite side effect.

    Stable runtimes retain the historical camelCase/underscore normalization,
    but path-shaped ids are rejected everywhere.  The AC dev plane is narrower:
    it accepts only the already-existing ``aming-claw`` project database.
    """

    raw = str(project_id or "").strip()
    normalized = validate_project_id_syntax(raw)
    if _is_dev_runtime() and raw != AC_PROJECT_ID:
        raise ValueError(
            "AC dev runtime project allowlist requires exact project_id="
            + AC_PROJECT_ID
        )
    plane = os.environ.get(RUNTIME_PLANE_ENV, "").strip().lower()
    if plane in {"stable", "generic"} and normalized == AC_PROJECT_ID:
        raise ValueError(f"{plane} runtime rejects the AC project domain")
    return normalized


def _external_read_path_identity(path: Path, *, kind: str) -> os.stat_result:
    """Resolve one existing non-symlink external discovery storage object."""

    absolute = path.absolute()
    try:
        before = absolute.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"registered {kind} does not exist") from exc
    if absolute.is_symlink():
        raise ValueError(f"registered {kind} cannot be a symlink")
    expected_mode = stat.S_ISDIR if kind == "project directory" else stat.S_ISREG
    if not expected_mode(before.st_mode):
        raise ValueError(f"registered {kind} has an invalid file type")
    if absolute.resolve(strict=True) != absolute:
        raise ValueError(f"registered {kind} escaped its canonical path")
    return before


def registered_public_safe_external_project(project_id: str) -> dict:
    """Resolve a registered public-safe project for dev read-only discovery.

    This reader intentionally bypasses ``project_service`` because that module's
    registry helper may create the registry parent.  Every path here must exist
    already and is opened without a write-capable helper.
    """

    if not _is_dev_runtime():
        raise RuntimeError("external read-only discovery is dev-plane only")
    _ = project_id
    raise ValueError(
        "AC dev external project discovery is retired; use the owning stable "
        "project service without a cross-world storage bridge"
    )
    # The code below is retained temporarily as unreachable archive logic so a
    # later cleanup can compare exact historical validation behavior.  No
    # runtime entry reaches it after the dual-world cutover.
    canonical = validate_project_id_syntax(project_id, require_exact=True)
    if canonical == AC_PROJECT_ID:
        raise ValueError("external discovery requires a non-AC project")
    root = _governance_root()
    _external_read_path_identity(root, kind="project directory")
    registry_path = root / "projects.json"
    _external_read_path_identity(registry_path, kind="project registry")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("registered project registry is unreadable") from exc
    projects = registry.get("projects") if isinstance(registry, Mapping) else None
    entry = projects.get(canonical) if isinstance(projects, Mapping) else None
    if not isinstance(entry, Mapping):
        raise ValueError("external project is not registered")
    if str(entry.get("project_id") or "") != canonical:
        raise ValueError("external project registry key does not match project_id")
    if entry.get("initialized") is not True:
        raise ValueError("external project is not initialized")
    if str(entry.get("status") or "").strip().lower() != "active":
        raise ValueError("external project is not active")
    config = entry.get("project_config")
    governance = config.get("governance") if isinstance(config, Mapping) else None
    policy = governance.get("policy") if isinstance(governance, Mapping) else None
    if not isinstance(policy, Mapping) or policy.get("public_safe") is not True:
        raise ValueError("external project is not registered public-safe")

    project_dir = root / canonical
    _external_read_path_identity(project_dir, kind="project directory")
    if project_dir.parent.resolve(strict=True) != root.resolve(strict=True):
        raise ValueError("registered project directory escaped governance root")
    db_path = project_dir / "governance.db"
    db_stat = _external_read_path_identity(db_path, kind="governance database")
    if db_path.parent.resolve(strict=True) != project_dir.resolve(strict=True):
        raise ValueError("registered governance database escaped project directory")
    for suffix in ("-wal", "-shm", "-journal"):
        companion = Path(str(db_path) + suffix)
        try:
            companion.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        _external_read_path_identity(
            companion,
            kind=f"SQLite companion {suffix}",
        )
    return {
        "project_id": canonical,
        "name": str(entry.get("name") or canonical),
        "status": "active",
        "initialized": True,
        "public_safe": True,
        "db_device": int(db_stat.st_dev),
        "db_inode": int(db_stat.st_ino),
        "storage_validated_without_database_open": True,
    }


def _resolve_project_dir(project_id: str) -> Path:
    """Resolve the actual project directory, handling normalize mismatch.

    Tries normalized ID first, then raw ID as fallback. This handles the case
    where data was created with the raw ID (e.g., 'amingClaw') before normalize
    was enforced (P0-1), so the directory on disk doesn't match the normalized
    form ('aming-claw').
    """
    normalized = validate_project_id(project_id)
    root = _governance_root()
    normalized_dir = root / normalized
    if _is_dev_runtime():
        if not normalized_dir.is_dir():
            raise FileNotFoundError(
                "AC dev runtime project directory must already exist: "
                + str(normalized_dir)
            )
        resolved_root = root.resolve(strict=True)
        resolved_project = normalized_dir.resolve(strict=True)
        if resolved_project.parent != resolved_root or resolved_project != normalized_dir:
            raise ValueError("AC dev runtime project directory identity mismatch")
        return normalized_dir
    if normalized_dir.exists():
        return normalized_dir
    # Fallback: try raw project_id (handles pre-normalize data)
    raw_dir = root / project_id
    if raw_dir.exists():
        return raw_dir
    # Neither exists — use normalized (will be created)
    return normalized_dir


def _project_db_path(project_id: str) -> Path:
    """Path to the SQLite database for a specific project."""
    project_dir = _resolve_project_dir(project_id)
    if _is_dev_runtime():
        db_path = project_dir / "governance.db"
        if not db_path.is_file():
            raise FileNotFoundError(
                "AC dev runtime governance database must already exist: "
                + str(db_path)
            )
        if db_path.is_symlink():
            raise ValueError("AC dev runtime governance database cannot be a symlink")
        resolved_db = db_path.resolve(strict=True)
        if resolved_db != db_path.absolute():
            raise ValueError("AC dev runtime governance database identity mismatch")
        if not stat.S_ISREG(db_path.stat(follow_symlinks=False).st_mode):
            raise ValueError("AC dev runtime governance database must be a regular file")
        return db_path
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir / "governance.db"


def _configure_connection(
    conn: sqlite3.Connection,
    busy_timeout: int,
    *,
    allow_journal_mode_write: bool = True,
) -> None:
    """Apply portable SQLite connection settings.

    Some shared-volume mounts reject WAL creation even when the DB file exists.
    In that case, fall back to DELETE journal mode so the governance service
    stays available instead of failing every request.
    """
    conn.row_factory = sqlite3.Row
    if allow_journal_mode_write:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.OperationalError:
                pass
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    except sqlite3.OperationalError:
        pass


def _verify_existing_schema(conn: sqlite3.Connection) -> None:
    """Fail closed when a dev-plane database is not already schema-compatible."""

    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        actual = int(row["value"]) if row else 0
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RuntimeError(
            "AC dev runtime requires an existing schema_meta version"
        ) from exc
    if actual != SCHEMA_VERSION:
        raise RuntimeError(
            "AC dev runtime schema mismatch: "
            f"expected {SCHEMA_VERSION}, found {actual}; use an isolated clone "
            "for migrations before explicit promotion"
        )


def _dev_schema_authorizer(
    action: int,
    arg1: str | None,
    _arg2: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    """Permit repair-row DML while denying live schema/attachment mutation."""

    if action in _DEV_DENIED_SCHEMA_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        if str(arg1 or "").lower() in {"schema_meta", "sqlite_master", "sqlite_schema"}:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect_existing(db_path: Path, *, timeout: float) -> sqlite3.Connection:
    absolute = db_path.absolute()
    before = absolute.stat(follow_symlinks=False)
    if absolute.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError("AC dev runtime governance database must be a non-symlink file")
    resolved = absolute.resolve(strict=True)
    if resolved != absolute:
        raise ValueError("AC dev runtime governance database escaped its canonical path")
    with _DEV_DATABASE_WRITER_LEASES_LOCK:
        lease_key = str(resolved)
        receipt = _DEV_DATABASE_WRITER_LEASES.get(lease_key)
        if not receipt or getattr(receipt.get("handle"), "closed", True):
            raise RuntimeError("AC dev governance database writer lease is not held")
        if (
            int(receipt.get("owner_pid") or 0) != os.getpid()
            or receipt.get("owner_start_identity") != _writer_process_start_identity()
            or (receipt.get("database_device"), receipt.get("database_inode"))
            != (int(before.st_dev), int(before.st_ino))
        ):
            raise RuntimeError("AC dev governance database writer lease binding mismatch")
    uri = absolute.as_uri() + "?mode=rw"
    conn = sqlite3.connect(uri, timeout=timeout, uri=True)
    database_file = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    after = absolute.stat(follow_symlinks=False)
    if (
        Path(database_file).resolve(strict=True) != absolute
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or absolute.is_symlink()
    ):
        conn.close()
        raise ValueError("AC dev runtime governance database identity changed during open")
    conn.set_authorizer(_dev_schema_authorizer)
    return conn


def get_connection(project_id: str) -> sqlite3.Connection:
    """Get a SQLite connection for a project, creating/migrating schema if needed.

    Returns:
        sqlite3.Connection: An open, fully-configured connection to the
        per-project governance database.

    Connection configuration applied on every call:

    WAL mode (PRAGMA journal_mode=WAL):
        Enables Write-Ahead Logging, which allows concurrent readers to proceed
        without being blocked by an active writer.  This is important because
        multiple agents may query the database simultaneously while a write
        transaction is in progress.

    Foreign-key enforcement (PRAGMA foreign_keys=ON):
        SQLite does not enforce foreign-key constraints by default; this PRAGMA
        activates referential-integrity checks for the lifetime of the
        connection (e.g. task_attempts.task_id → tasks.task_id).

    Busy timeout (PRAGMA busy_timeout=5000):
        Instructs SQLite to wait up to 5 000 ms before raising
        ``OperationalError: database is locked`` when another connection holds
        an exclusive lock.  This prevents spurious failures under brief write
        contention.

    Row factory (sqlite3.Row):
        Sets ``conn.row_factory = sqlite3.Row`` so that every fetched row
        supports both index-based and column-name-based access
        (``row["column_name"]`` as well as ``row[0]``).

    Auto-schema migration (_ensure_schema):
        ``_ensure_schema(conn)`` is called on every new connection.  It runs
        the full ``SCHEMA_SQL`` block (``CREATE TABLE IF NOT EXISTS …``) to
        create tables on first use, then checks the stored ``schema_version``
        against ``SCHEMA_VERSION`` (currently {version}) and runs any
        outstanding incremental migration functions up to that target version.
        This means callers never need to manage schema lifecycle manually.
    """.format(version=SCHEMA_VERSION)
    db_path = _project_db_path(project_id)

    # On Docker restart, stale WAL locks may block new connections.
    # SQLite automatically recovers WAL state on first connect, but only
    # if the -shm file is accessible. Increase timeout to handle this.
    if _is_dev_runtime():
        conn = _connect_existing(db_path, timeout=30)
        try:
            _verify_existing_schema(conn)
            _configure_connection(
                conn,
                busy_timeout=10000,
                allow_journal_mode_write=False,
            )
        except Exception:
            conn.close()
            raise
        return conn

    conn = sqlite3.connect(str(db_path), timeout=30)
    _configure_connection(conn, busy_timeout=10000)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    """Create all required tables if they do not already exist, then run any pending migrations.

    On first use, executes the full ``SCHEMA_SQL`` block (``CREATE TABLE IF NOT
    EXISTS …``) to initialise every table and index in the governance database.
    Subsequent calls are safe because every statement uses ``IF NOT EXISTS``.

    After the baseline schema is applied, the stored ``schema_version`` value is
    read from the ``schema_meta`` table and compared against the module-level
    ``SCHEMA_VERSION`` constant.  For each version step between the current and
    target version, the corresponding incremental migration function is executed
    in order to bring the schema up to date (e.g. adding new columns, creating
    new indexes, or back-filling data).  When all pending migrations have run,
    the stored version is updated to reflect the new baseline.
    """
    conn.executescript(SCHEMA_SQL)

    # Check and set schema version
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        current_version = 0

    if current_version < SCHEMA_VERSION:
        _run_migrations(conn, current_version, SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def _run_migrations(conn: sqlite3.Connection, from_version: int, to_version: int):
    """Run incremental migrations between versions.

    Add migration functions as the schema evolves:
        MIGRATIONS = {
            1: _migrate_v0_to_v1,
            2: _migrate_v1_to_v2,
        }
    """
    def _migrate_v1_to_v2(c):
        """Add new columns to tasks table + event_outbox + task_attempts."""
        # Add missing columns to tasks (ALTER TABLE ADD is safe for existing data)
        for col, typedef in [
            ("type", "TEXT NOT NULL DEFAULT 'task'"),
            ("prompt", "TEXT"),
            ("assigned_to", "TEXT"),
            ("started_at", "TEXT"),
            ("completed_at", "TEXT"),
            ("result_json", "TEXT"),
            ("error_message", "TEXT"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("metadata_json", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Create task_attempts table if not exists
        c.execute("""CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_num INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            result_json TEXT,
            error_message TEXT
        )""")

        # Create event_outbox if not exists (may already be from schema)
        c.execute("""CREATE TABLE IF NOT EXISTS event_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            project_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            dead_letter INTEGER NOT NULL DEFAULT 0,
            trace_id TEXT
        )""")

        # Create indexes
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(project_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to, status)")
        except sqlite3.OperationalError:
            pass

    def _migrate_v2_to_v3(c):
        """Add dual-field status model to tasks."""
        for col, typedef in [
            ("execution_status", "TEXT NOT NULL DEFAULT 'queued'"),
            ("notification_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("notified_at", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        # Sync execution_status from status for existing rows
        try:
            c.execute("UPDATE tasks SET execution_status = status WHERE execution_status = 'queued' AND status != 'queued'")
        except sqlite3.OperationalError:
            pass

    def _migrate_v3_to_v4(c):
        """Add retry_round and parent_task_id fields to tasks for QA→Dev escalation."""
        for col, typedef in [
            ("retry_round", "INTEGER NOT NULL DEFAULT 0"),
            ("parent_task_id", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v4_to_v5(c):
        """Add project_version table for chain integrity seal."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS project_version (
                project_id    TEXT PRIMARY KEY,
                chain_version TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                updated_by    TEXT NOT NULL
            )
        """)

    def _migrate_v5_to_v6(c):
        """Add git sync columns to project_version (executor writes git status)."""
        for col, typedef in [
            ("git_head", "TEXT DEFAULT ''"),
            ("dirty_files", "TEXT DEFAULT '[]'"),
            ("git_synced_at", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE project_version ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

    def _migrate_v6_to_v7(c):
        """Add memories table with FTS5 full-text search for Phase 2 memory backend."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                memory_id   TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                ref_id      TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL DEFAULT 'knowledge',
                module_id   TEXT NOT NULL DEFAULT '',
                scope       TEXT NOT NULL DEFAULT 'project',
                content     TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL DEFAULT '',
                metadata_json TEXT,
                tags        TEXT NOT NULL DEFAULT '',
                version     INTEGER NOT NULL DEFAULT 1,
                status      TEXT NOT NULL DEFAULT 'active',
                superseded_by_memory_id TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_ref ON memories(project_id, ref_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_status ON memories(project_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_module ON memories(project_id, module_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(project_id, kind)")

        # FTS5 virtual table for full-text search
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, summary, module_id, kind,
                content='memories',
                content_rowid='rowid'
            )
        """)

        # FTS5 sync triggers: keep FTS index in sync with memories table
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, summary, module_id, kind)
                VALUES (new.rowid, new.content, new.summary, new.module_id, new.kind);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, summary, module_id, kind)
                VALUES ('delete', old.rowid, old.content, old.summary, old.module_id, old.kind);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, summary, module_id, kind)
                VALUES ('delete', old.rowid, old.content, old.summary, old.module_id, old.kind);
                INSERT INTO memories_fts(rowid, content, summary, module_id, kind)
                VALUES (new.rowid, new.content, new.summary, new.module_id, new.kind);
            END
        """)

        # Memory relations table
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_ref_id TEXT NOT NULL,
                relation    TEXT NOT NULL,
                to_ref_id   TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                metadata_json TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memrel_from ON memory_relations(project_id, from_ref_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memrel_to ON memory_relations(project_id, to_ref_id)")

        # Memory events table (audit trail for memory lifecycle)
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_id      TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                actor_id    TEXT NOT NULL DEFAULT '',
                detail      TEXT NOT NULL DEFAULT '',
                metadata_json TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memevt_ref ON memory_events(project_id, ref_id)")

    def _migrate_v7_to_v8(c):
        """Phase 3: Add entity_id column for ref_id↔entity mapping."""
        try:
            c.execute("ALTER TABLE memories ADD COLUMN entity_id TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_entity ON memories(project_id, entity_id)")

    def _migrate_v8_to_v9(c):
        """Add observer_mode flag to project_version for observer takeover support."""
        try:
            c.execute("ALTER TABLE project_version ADD COLUMN observer_mode INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v9_to_v10(c):
        """Add session_context table for coordinator session-level logging."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS session_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                task_id TEXT,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                created_by TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_context_project
            ON session_context(project_id, created_at)
        """)

    def _migrate_v10_to_v11(c):
        """Add trace_id and chain_id columns to tasks table for end-to-end chain tracing."""
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN trace_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN chain_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_trace ON tasks(project_id, trace_id)")

    def _migrate_v11_to_v12(c):
        """Add gate_events table for queryable gate audit trail."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS gate_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT NOT NULL,
                task_id       TEXT NOT NULL,
                gate_name     TEXT NOT NULL,
                passed        INTEGER NOT NULL,
                reason        TEXT,
                trace_id      TEXT,
                created_at    TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_gate_events_project_task ON gate_events(project_id, task_id)")

    def _migrate_v12_to_v13(c):
        """Add subtask_groups table, subtask columns to tasks, max_subtasks to project_version."""
        # subtask_groups table
        c.execute("""
            CREATE TABLE IF NOT EXISTS subtask_groups (
                group_id       TEXT PRIMARY KEY,
                project_id     TEXT NOT NULL,
                pm_task_id     TEXT NOT NULL,
                total_count    INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                status         TEXT NOT NULL DEFAULT 'active',
                created_at     TEXT NOT NULL,
                completed_at   TEXT,
                trace_id       TEXT,
                chain_id       TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_subtask_groups_project ON subtask_groups(project_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subtask_groups_pm ON subtask_groups(pm_task_id)")

        # Add subtask columns to tasks table
        for col, typedef in [
            ("subtask_group_id", "TEXT"),
            ("subtask_local_id", "TEXT"),
            ("subtask_depends_on", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Add max_subtasks to project_version
        try:
            c.execute("ALTER TABLE project_version ADD COLUMN max_subtasks INTEGER NOT NULL DEFAULT 5")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v13_to_v14(c):
        """Add pending_nodes table for inferred doc associations (P4)."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_nodes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT NOT NULL,
                node_id       TEXT NOT NULL,
                doc_path      TEXT NOT NULL,
                confidence    REAL NOT NULL DEFAULT 0.0,
                reason        TEXT,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                reviewed_at   TEXT,
                reviewed_by   TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_nodes_project ON pending_nodes(project_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_nodes_node ON pending_nodes(project_id, node_id)")

    def _migrate_v14_to_v15(c):
        """Add backlog_bugs table for DB-first backlog storage (OPT-DB-BACKLOG)."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS backlog_bugs (
                bug_id              TEXT PRIMARY KEY,
                title               TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'OPEN',
                priority            TEXT NOT NULL DEFAULT 'P3',
                target_files        TEXT NOT NULL DEFAULT '[]',
                test_files          TEXT NOT NULL DEFAULT '[]',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                chain_task_id       TEXT NOT NULL DEFAULT '',
                "commit"            TEXT NOT NULL DEFAULT '',
                discovered_at       TEXT NOT NULL DEFAULT '',
                fixed_at            TEXT NOT NULL DEFAULT '',
                details_md          TEXT NOT NULL DEFAULT '',
                chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
                required_docs       TEXT NOT NULL DEFAULT '[]',
                provenance_paths    TEXT NOT NULL DEFAULT '[]',
                chain_stage         TEXT NOT NULL DEFAULT '',
                last_failure_reason TEXT NOT NULL DEFAULT '',
                stage_updated_at    TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_backlog_bugs_status ON backlog_bugs(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_backlog_bugs_priority ON backlog_bugs(priority)")

    def _migrate_v15_to_v16(c):
        """R1: Add resolution_commit and resolution_summary columns to memories table."""
        for col, typedef in [
            ("resolution_commit", "TEXT DEFAULT ''"),
            ("resolution_summary", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v16_to_v17(c):
        """Add required_docs column to backlog_bugs table for structured doc references."""
        try:
            c.execute("ALTER TABLE backlog_bugs ADD COLUMN required_docs TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v17_to_v18(c):
        """Add provenance_paths column to backlog_bugs for tracking source doc paths."""
        try:
            c.execute("ALTER TABLE backlog_bugs ADD COLUMN provenance_paths TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v18_to_v19(c):
        """Add version_baselines table for Phase I baseline storage."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS version_baselines (
                project_id        TEXT NOT NULL,
                baseline_id       INTEGER NOT NULL,
                chain_version     TEXT NOT NULL,
                graph_sha         TEXT NOT NULL DEFAULT '',
                code_doc_map_sha  TEXT NOT NULL DEFAULT '',
                node_state_snap   TEXT NOT NULL DEFAULT '{}',
                chain_event_max   INTEGER NOT NULL DEFAULT 0,
                trigger           TEXT NOT NULL DEFAULT '',
                triggered_by      TEXT NOT NULL DEFAULT '',
                reconstructed     INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL,
                notes             TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, baseline_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_baselines_chain_version ON version_baselines(project_id, chain_version)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_baselines_created_at ON version_baselines(project_id, created_at)")

    def _migrate_v19_to_v20(c):
        """Add phase_h_processed_symbols table for Phase H content delta detection."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS phase_h_processed_symbols (
                fingerprint       TEXT PRIMARY KEY,
                project_id        TEXT NOT NULL,
                commit_sha        TEXT NOT NULL,
                symbol_kind       TEXT NOT NULL,
                symbol_qname      TEXT NOT NULL,
                expected_doc      TEXT NOT NULL,
                spawned_task_id   TEXT NOT NULL DEFAULT '',
                spawn_status      TEXT NOT NULL DEFAULT 'pending',
                last_chain_event  TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL,
                processed_at      TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_phase_h_processed_status ON phase_h_processed_symbols(project_id, spawn_status)")

    def _migrate_v20_to_v21(c):
        """Add phase_k_processed_contracts table for Phase K autospawn dedup."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS phase_k_processed_contracts (
                fingerprint       TEXT PRIMARY KEY,
                contract_kind     TEXT NOT NULL,
                contract_id       TEXT NOT NULL,
                discrepancy_type  TEXT NOT NULL,
                target_doc        TEXT NOT NULL DEFAULT '',
                target_test       TEXT NOT NULL DEFAULT '',
                spawned_task_id   TEXT NOT NULL DEFAULT '',
                spawn_status      TEXT NOT NULL DEFAULT 'pending',
                last_chain_event  TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL,
                processed_at      TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_phase_k_processed_status ON phase_k_processed_contracts(spawn_status)")

    def _migrate_v21_to_v22(c):
        """Add slice-baseline columns to version_baselines and create baseline_mutations table."""
        # R1: Extend version_baselines with 7 new nullable columns (idempotent)
        for col, typedef in [
            ("scope_id", "TEXT"),
            ("parent_baseline_id", "INTEGER"),
            ("scope_kind", "TEXT"),
            ("scope_value", "TEXT"),
            ("merged_into", "INTEGER"),
            ("merge_status", "TEXT"),
            ("merge_evidence_json", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE version_baselines ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # R2: Create baseline_mutations table
        c.execute("""
            CREATE TABLE IF NOT EXISTS baseline_mutations (
                project_id      TEXT NOT NULL,
                baseline_id     INTEGER NOT NULL,
                mutation_id     TEXT NOT NULL,
                mutation_type   TEXT NOT NULL DEFAULT '',
                affected_file   TEXT NOT NULL DEFAULT '',
                affected_node   TEXT NOT NULL DEFAULT '',
                before_sha256   TEXT NOT NULL DEFAULT '',
                after_sha256    TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, baseline_id, mutation_id),
                FOREIGN KEY (project_id, baseline_id) REFERENCES version_baselines(project_id, baseline_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_project ON baseline_mutations(project_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_baseline ON baseline_mutations(project_id, baseline_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_file ON baseline_mutations(affected_file)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_node ON baseline_mutations(affected_node)")

    def _migrate_v22_to_v23(c):
        """Add chain_stage, last_failure_reason, stage_updated_at columns to backlog_bugs."""
        for col, typedef in [
            ("chain_stage", "TEXT NOT NULL DEFAULT ''"),
            ("last_failure_reason", "TEXT NOT NULL DEFAULT ''"),
            ("stage_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE backlog_bugs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v23_to_v24(c):
        """Add mutations_sha256 column to version_baselines for per-baseline mutation fingerprints."""
        try:
            c.execute("ALTER TABLE version_baselines ADD COLUMN mutations_sha256 TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v24_to_v25(c):
        """Add migrations table for Phase Z v2 migration state machine."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                project_id        TEXT PRIMARY KEY,
                started_at        TEXT,
                deadline_at       TEXT,
                owner             TEXT,
                state             TEXT,
                current_extension INTEGER DEFAULT 0,
                abort_reason      TEXT
            )
        """)

    def _migrate_v25_to_v26(c):
        """Add reconcile_sessions table + partial UNIQUE INDEX (CR0a).

        Mirrors SCHEMA_SQL: one-active-per-project invariant enforced via the
        ``idx_reconcile_sessions_one_active`` partial unique index over
        (project_id) WHERE status IN ('active','finalizing').
        """
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_sessions (
                project_id              TEXT NOT NULL,
                session_id              TEXT NOT NULL,
                run_id                  TEXT,
                status                  TEXT NOT NULL DEFAULT 'active'
                                          CHECK (status IN ('active','finalizing','finalized','rolled_back')),
                started_at              TEXT NOT NULL,
                finalized_at            TEXT,
                cluster_count_total     INTEGER NOT NULL DEFAULT 0,
                cluster_count_resolved  INTEGER NOT NULL DEFAULT 0,
                cluster_count_failed    INTEGER NOT NULL DEFAULT 0,
                bypass_gates_json       TEXT NOT NULL DEFAULT '[]',
                started_by              TEXT,
                snapshot_path           TEXT,
                snapshot_head_sha       TEXT,
                PRIMARY KEY (project_id, session_id)
            )
        """)
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_sessions_one_active "
            "ON reconcile_sessions (project_id) "
            "WHERE status IN ('active','finalizing')"
        )

    def _migrate_v26_to_v27(c):
        """Backlog-owned chain/MF runtime state mirror."""
        for col, typedef in [
            ("runtime_state", "TEXT NOT NULL DEFAULT ''"),
            ("current_task_id", "TEXT NOT NULL DEFAULT ''"),
            ("root_task_id", "TEXT NOT NULL DEFAULT ''"),
            ("worktree_path", "TEXT NOT NULL DEFAULT ''"),
            ("worktree_branch", "TEXT NOT NULL DEFAULT ''"),
            ("bypass_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("runtime_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE backlog_bugs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v27_to_v28(c):
        """MF profile and chain takeover audit fields."""
        for col, typedef in [
            ("mf_type", "TEXT NOT NULL DEFAULT ''"),
            ("takeover_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            try:
                c.execute(f"ALTER TABLE backlog_bugs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v28_to_v29(c):
        """Add reconcile batch memory for PM semantic merge context."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_batch_memory (
                project_id       TEXT NOT NULL,
                batch_id         TEXT NOT NULL,
                session_id       TEXT NOT NULL DEFAULT '',
                status           TEXT NOT NULL DEFAULT 'active',
                memory_json      TEXT NOT NULL DEFAULT '{}',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                created_by       TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, batch_id)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_session "
            "ON reconcile_batch_memory (project_id, session_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_status "
            "ON reconcile_batch_memory (project_id, status)"
        )

    def _migrate_v29_to_v30(c):
        """Add reconcile file inventory coverage ledger."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_file_inventory (
                project_id        TEXT NOT NULL,
                run_id            TEXT NOT NULL,
                path              TEXT NOT NULL,
                file_kind         TEXT NOT NULL DEFAULT '',
                language          TEXT NOT NULL DEFAULT '',
                sha256            TEXT NOT NULL DEFAULT '',
                file_hash         TEXT NOT NULL DEFAULT '',
                size_bytes        INTEGER NOT NULL DEFAULT 0,
                last_scanned_commit TEXT NOT NULL DEFAULT '',
                graph_status      TEXT NOT NULL DEFAULT '',
                mapped_node_ids   TEXT NOT NULL DEFAULT '[]',
                attached_node_ids TEXT NOT NULL DEFAULT '[]',
                attachment_role   TEXT NOT NULL DEFAULT '',
                attachment_source TEXT NOT NULL DEFAULT '',
                scan_status       TEXT NOT NULL DEFAULT '',
                cluster_id        TEXT NOT NULL DEFAULT '',
                candidate_node_id TEXT NOT NULL DEFAULT '',
                attached_to       TEXT NOT NULL DEFAULT '',
                reason            TEXT NOT NULL DEFAULT '',
                decision          TEXT NOT NULL DEFAULT 'pending',
                updated_at        TEXT NOT NULL,
                PRIMARY KEY (project_id, run_id, path)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_status "
            "ON reconcile_file_inventory (project_id, run_id, scan_status)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_kind "
            "ON reconcile_file_inventory (project_id, run_id, file_kind)"
        )

    def _migrate_v30_to_v31(c):
        """Add reconcile commit baseline and retryable finalize failure state."""
        c.execute("DROP INDEX IF EXISTS idx_reconcile_sessions_one_active")
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_sessions_v31 (
                project_id              TEXT NOT NULL,
                session_id              TEXT NOT NULL,
                run_id                  TEXT,
                status                  TEXT NOT NULL DEFAULT 'active'
                                          CHECK (status IN ('active','finalizing','finalize_failed','finalized','rolled_back')),
                started_at              TEXT NOT NULL,
                finalized_at            TEXT,
                cluster_count_total     INTEGER NOT NULL DEFAULT 0,
                cluster_count_resolved  INTEGER NOT NULL DEFAULT 0,
                cluster_count_failed    INTEGER NOT NULL DEFAULT 0,
                bypass_gates_json       TEXT NOT NULL DEFAULT '[]',
                started_by              TEXT,
                snapshot_path           TEXT,
                snapshot_head_sha       TEXT,
                base_commit_sha         TEXT NOT NULL DEFAULT '',
                finalize_error_json     TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (project_id, session_id)
            )
        """)
        columns = {
            row[1] for row in c.execute("PRAGMA table_info(reconcile_sessions)").fetchall()
        }
        base_expr = "base_commit_sha" if "base_commit_sha" in columns else "COALESCE(snapshot_head_sha, '')"
        err_expr = "finalize_error_json" if "finalize_error_json" in columns else "'{}'"
        c.execute(f"""
            INSERT OR REPLACE INTO reconcile_sessions_v31 (
                project_id, session_id, run_id, status, started_at, finalized_at,
                cluster_count_total, cluster_count_resolved, cluster_count_failed,
                bypass_gates_json, started_by, snapshot_path, snapshot_head_sha,
                base_commit_sha, finalize_error_json
            )
            SELECT
                project_id, session_id, run_id, status, started_at, finalized_at,
                cluster_count_total, cluster_count_resolved, cluster_count_failed,
                bypass_gates_json, started_by, snapshot_path, snapshot_head_sha,
                {base_expr}, {err_expr}
            FROM reconcile_sessions
        """)
        c.execute("DROP TABLE reconcile_sessions")
        c.execute("ALTER TABLE reconcile_sessions_v31 RENAME TO reconcile_sessions")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_sessions_one_active "
            "ON reconcile_sessions (project_id) "
            "WHERE status IN ('active','finalizing','finalize_failed')"
        )

    def _migrate_v31_to_v32(c):
        """Add branch-isolated reconcile target provenance."""
        for col, typedef in [
            ("target_branch", "TEXT NOT NULL DEFAULT ''"),
            ("target_head_sha", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE reconcile_sessions ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass

    def _migrate_v32_to_v33(c):
        """Add explicit file hash and graph mapping drift fields."""
        for col, typedef in [
            ("file_hash", "TEXT NOT NULL DEFAULT ''"),
            ("size_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("last_scanned_commit", "TEXT NOT NULL DEFAULT ''"),
            ("graph_status", "TEXT NOT NULL DEFAULT ''"),
            ("mapped_node_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ]:
            try:
                c.execute(f"ALTER TABLE reconcile_file_inventory ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        try:
            c.execute(
                "UPDATE reconcile_file_inventory "
                "SET file_hash = CASE WHEN sha256 != '' THEN 'sha256:' || sha256 ELSE '' END "
                "WHERE file_hash = ''"
            )
        except sqlite3.OperationalError:
            pass

    def _migrate_v33_to_v34(c):
        """Add commit-indexed graph snapshot state tables."""
        from .graph_snapshot_store import GRAPH_SNAPSHOT_SCHEMA_SQL

        c.executescript(GRAPH_SNAPSHOT_SCHEMA_SQL)

    def _migrate_v34_to_v35(c):
        """Add explicit graph attachment metadata to file inventory rows."""
        for col, typedef in [
            ("attached_node_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("attachment_role", "TEXT NOT NULL DEFAULT ''"),
            ("attachment_source", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE reconcile_file_inventory ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass

    def _migrate_v35_to_v36(c):
        """Add durable parallel branch runtime context state."""
        from .parallel_branch_runtime import ensure_branch_runtime_schema

        ensure_branch_runtime_schema(c)

    def _migrate_v36_to_v37(c):
        """Add durable parallel branch merge queue item state."""
        from .parallel_branch_runtime import ensure_branch_runtime_schema

        ensure_branch_runtime_schema(c)

    def _migrate_v37_to_v38(c):
        """Add durable parallel branch batch rollback state."""
        from .parallel_branch_runtime import ensure_branch_runtime_schema

        ensure_branch_runtime_schema(c)

    def _migrate_v38_to_v39(c):
        """Add managed ref runtime for existing long-lived branches."""
        from .managed_ref_runtime import ensure_managed_ref_schema

        ensure_managed_ref_schema(c)

    def _migrate_v39_to_v40(c):
        """Add durable structured AI output intake tables."""
        from .ai_output_intake import ensure_schema as ensure_ai_output_intake_schema

        ensure_ai_output_intake_schema(c)

    def _migrate_v40_to_v41(c):
        """Add append-only task timeline evidence table."""
        from .task_timeline import ensure_schema as ensure_task_timeline_schema

        ensure_task_timeline_schema(c)

    def _migrate_v41_to_v42(c):
        """Add MF scenario/gate evidence fields to task timeline."""
        from .task_timeline import ensure_schema as ensure_task_timeline_schema

        ensure_task_timeline_schema(c)

    def _migrate_v42_to_v43(c):
        """Add unified graph asset projection tables for doc/test/config state."""
        from .asset_projection import ensure_schema as ensure_asset_projection_schema

        ensure_asset_projection_schema(c)

    def _migrate_v43_to_v44(c):
        """Add asset impact event log and pending reminder projection."""
        from .asset_impact import ensure_schema as ensure_asset_impact_schema

        ensure_asset_impact_schema(c)

    def _migrate_v44_to_v45(c):
        """Add Project Inbox raw requirement capture table."""
        from .raw_requirement import ensure_schema as ensure_raw_requirement_schema

        ensure_raw_requirement_schema(c)

    def _migrate_v45_to_v46(c):
        """Add registered observer sessions and observer command queue."""
        from .observer_session import ensure_schema as ensure_observer_session_schema

        ensure_observer_session_schema(c)

    def _migrate_v46_to_v47(c):
        """Add role-scoped context registry tables."""
        from .context_registry import ensure_schema as ensure_context_registry_schema

        ensure_context_registry_schema(c)

    MIGRATIONS = {2: _migrate_v1_to_v2, 3: _migrate_v2_to_v3, 4: _migrate_v3_to_v4, 5: _migrate_v4_to_v5, 6: _migrate_v5_to_v6, 7: _migrate_v6_to_v7, 8: _migrate_v7_to_v8, 9: _migrate_v8_to_v9, 10: _migrate_v9_to_v10, 11: _migrate_v10_to_v11, 12: _migrate_v11_to_v12, 13: _migrate_v12_to_v13, 14: _migrate_v13_to_v14, 15: _migrate_v14_to_v15, 16: _migrate_v15_to_v16, 17: _migrate_v16_to_v17, 18: _migrate_v17_to_v18, 19: _migrate_v18_to_v19, 20: _migrate_v19_to_v20, 21: _migrate_v20_to_v21, 22: _migrate_v21_to_v22, 23: _migrate_v22_to_v23, 24: _migrate_v23_to_v24, 25: _migrate_v24_to_v25, 26: _migrate_v25_to_v26, 27: _migrate_v26_to_v27, 28: _migrate_v27_to_v28, 29: _migrate_v28_to_v29, 30: _migrate_v29_to_v30, 31: _migrate_v30_to_v31, 32: _migrate_v31_to_v32, 33: _migrate_v32_to_v33, 34: _migrate_v33_to_v34, 35: _migrate_v34_to_v35, 36: _migrate_v35_to_v36, 37: _migrate_v36_to_v37, 38: _migrate_v37_to_v38, 39: _migrate_v38_to_v39, 40: _migrate_v39_to_v40, 41: _migrate_v40_to_v41, 42: _migrate_v41_to_v42, 43: _migrate_v42_to_v43, 44: _migrate_v43_to_v44, 45: _migrate_v44_to_v45, 46: _migrate_v45_to_v46, 47: _migrate_v46_to_v47}
    for version in range(from_version + 1, to_version + 1):
        if version in MIGRATIONS:
            MIGRATIONS[version](conn)


def independent_connection(project_id: str, busy_timeout: int = 5000) -> sqlite3.Connection:
    """Open a *fresh* SQLite connection that bypasses any shared-connection pool.

    This is the preferred helper for write-heavy, latency-sensitive paths such
    as ``handle_version_update`` and ``handle_version_sync`` where a long-lived
    shared connection may already hold a WAL read-lock that causes the incoming
    write to block indefinitely.

    Key differences from ``get_connection``:
    * ``busy_timeout`` defaults to **5 000 ms** (vs 10 000 ms for the shared
      connection).  The tighter budget prevents a stalled write from blocking
      the HTTP worker thread for too long; callers are expected to wrap the call
      with the :func:`retry_on_busy` helper.
    * ``_ensure_schema`` is **not** called — the database is assumed to be
      fully migrated already.  This makes the helper cheap: no schema introspection,
      no migration logic, just open → configure → return.

    Args:
        project_id: Governance project identifier (used to locate the DB file).
        busy_timeout: SQLite busy_timeout in milliseconds (default 5000).

    Returns:
        An open, fully-configured ``sqlite3.Connection`` with WAL mode,
        foreign-key enforcement, the given busy_timeout, and ``Row`` factory.
    """
    db_path = _project_db_path(project_id)
    if _is_dev_runtime():
        conn = _connect_existing(db_path, timeout=busy_timeout / 1000.0)
        try:
            _verify_existing_schema(conn)
            _configure_connection(
                conn,
                busy_timeout=busy_timeout,
                allow_journal_mode_write=False,
            )
        except Exception:
            conn.close()
            raise
        return conn

    conn = sqlite3.connect(str(db_path), timeout=busy_timeout / 1000.0)
    _configure_connection(conn, busy_timeout=busy_timeout)
    return conn


def close_connection(conn: sqlite3.Connection):
    """Close a database connection."""
    if conn:
        conn.close()


class DBContext:
    """Context manager for database connections with automatic commit/rollback."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.conn = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = get_connection(self.project_id)
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            close_connection(self.conn)
        return False  # Don't suppress exceptions
