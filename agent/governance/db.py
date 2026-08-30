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

_SQLITE_WRITE_LOCK = threading.RLock()
_DEV_DATABASE_WRITER_LEASES: dict[str, dict[str, object]] = {}
_DEV_DATABASE_WRITER_LEASES_LOCK = threading.RLock()

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


def _dev_storage_root(*, create: bool = False) -> Path:
    raw = os.environ.get(AC_DEV_STORAGE_ROOT_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            "AC dev runtime requires an explicit AMING_CLAW_DEV_STORAGE_ROOT"
        )
    return _absolute_non_symlink_root(Path(raw), create=create)


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
    if project_id != AC_PROJECT_ID or port != 40008:
        raise ValueError("AC dev launch receipt requires exact project and port")
    if root == stable or stable in root.parents or root in stable.parents:
        raise ValueError("AC dev launch receipt root must be disjoint from stable volume")
    from agent.runtime_plane import resolve_ac_dev_storage_root
    if root != resolve_ac_dev_storage_root(stable):
        raise ValueError("AC dev launch receipt root must be canonical resolver output")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(source_sha256 or "")):
        raise ValueError("AC dev launch receipt source hash is invalid")
    root_stat, parent_stat = root.stat(follow_symlinks=False), stable.parent.stat(follow_symlinks=False)
    receipt = {
        "schema_version": AC_DEV_LAUNCH_RECEIPT_SCHEMA,
        "world_id": AC_DEV_WORLD_ID, "project_id": AC_PROJECT_ID,
        "runtime_plane": DEV_RUNTIME_PLANE, "port": 40008, "background": False,
        "storage_root": str(root), "storage_device": int(root_stat.st_dev), "storage_inode": int(root_stat.st_ino),
        "stable_shared_volume": str(stable),
        "stable_parent_device": int(parent_stat.st_dev), "stable_parent_inode": int(parent_stat.st_ino),
        "source_sha256": str(source_sha256),
    }
    path = root / AC_DEV_LAUNCH_RECEIPT_NAME
    if path.exists() and path.is_symlink():
        raise ValueError("AC dev launch receipt cannot be a symlink")
    temporary = root / (AC_DEV_LAUNCH_RECEIPT_NAME + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)
    return receipt


def validate_dev_launch_receipt(storage_root: Path | str, *, source_sha256: str) -> dict[str, object]:
    """Fail closed before a dev server opens SQLite or takes the writer lease."""
    root = _absolute_non_symlink_root(Path(storage_root), create=False)
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
    stable = Path(str(receipt.get("stable_shared_volume") or ""))
    if not stable.is_absolute() or stable.is_symlink() or not stable.is_dir() or stable.resolve(strict=True) != stable:
        raise ValueError("AC dev launch receipt stable identity invalid")
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
    if candidate_root != previous_root:
        raise ValueError("AC dev source upgrade root mismatch")
    if (
        str(previous.get("branch") or "") != "codex/ac-dev"
        or str(candidate.get("branch") or "") != "codex/ac-dev"
    ):
        raise ValueError("AC dev source upgrade branch mismatch")

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=candidate_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("AC dev source upgrade Git identity unavailable")
        return result.stdout.strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve(strict=True) != candidate_root:
        raise ValueError("AC dev source upgrade top-level mismatch")
    if git("branch", "--show-current") != "codex/ac-dev":
        raise ValueError("AC dev source upgrade checked-out branch mismatch")
    if git("status", "--porcelain"):
        raise ValueError("AC dev source upgrade requires a clean worktree")
    if git("rev-parse", "HEAD").lower() != str(candidate.get("commit") or ""):
        raise ValueError("AC dev source upgrade HEAD mismatch")
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


def _verify_dev_world_schema_inventory(conn: sqlite3.Connection) -> None:
    required, allowed, source_objects = _source_schema_table_contract()
    actual = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    unknown = sorted(actual - allowed)
    missing = sorted(required - actual)
    actual_objects = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
    }
    optional = allowed - required
    unknown_objects = sorted(
        item
        for item in actual_objects - source_objects
        if not (item[0] in {"table", "index"} and item[2] in optional)
    )
    if unknown or missing or unknown_objects:
        raise ValueError(
            "AC dev source schema inventory mismatch: "
            f"unknown={unknown}, missing={missing}, unknown_objects={unknown_objects}"
        )


def bootstrap_dev_governance_store(
    storage_root: Path | str,
    *,
    source_identity: Mapping[str, object],
    process_identity: Mapping[str, object],
    expected_source_tip_sha256: str = "",
    expected_previous_process_identity: Mapping[str, object] | None = None,
    expected_database_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Create or verify the source-only AC dev world without copying stable rows.

    This is the sole schema/genesis bootstrap.  Ordinary dev connections remain
    verify-only, so a later source change cannot silently migrate a running
    world.  A restart may re-read an exact existing genesis but cannot replace
    it or import any stable governance bytes.
    """

    root_input = Path(storage_root).expanduser().absolute()
    if root_input.is_symlink():
        raise ValueError("AC dev storage root cannot be a symlink")
    root_existed = root_input.exists()
    shared_raw = os.environ.get("SHARED_VOLUME_PATH", "").strip()
    if shared_raw:
        shared = Path(shared_raw).expanduser().absolute()
        if root_input == shared or root_input in shared.parents or shared in root_input.parents:
            raise ValueError("AC dev storage root must be disjoint from shared storage")
    root = _absolute_non_symlink_root(root_input, create=not root_input.exists())
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
    if root_existed and not database.exists():
        raise ValueError("AC dev fresh bootstrap requires an absent dedicated root")
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.parent.is_symlink() or database.parent.resolve(strict=True) != database.parent:
        raise ValueError("AC dev governance directory cannot be a symlink")
    companions = [
        candidate
        for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(str(database) + suffix)).exists()
    ]
    if companions:
        raise ValueError("AC dev bootstrap rejects reused WAL/SHM/journal state")
    lease_created = str(database.absolute()) not in _DEV_DATABASE_WRITER_LEASES
    acquire_dev_runtime_writer_lease(root)
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
    created = not database.exists()
    if database.is_symlink():
        raise ValueError("AC dev governance database cannot be a symlink")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(database), timeout=30)
        conn.row_factory = sqlite3.Row
        if created:
            _configure_connection(conn, busy_timeout=10000)
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
                stored_database.get("device") != int(current_database.st_dev)
                or stored_database.get("inode") != int(current_database.st_ino)
                or stored_root.get("path") != str(root)
                or stored_root.get("device") != int(current_root.st_dev)
                or stored_root.get("inode") != int(current_root.st_ino)
            ):
                raise ValueError("existing AC dev world storage/database identity changed")
            required_tables, _allowed_tables, _source_objects = _source_schema_table_contract()
            if stored_genesis.get("source_schema_sha256") != _source_schema_inventory_hash(
                conn, required_tables
            ):
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
                _verify_dev_source_upgrade(source_tip_identity, source)
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
        ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fp"],
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
        "source_commit": (
            os.environ.get("AMING_CLAW_STABLE_ANCHOR_COMMIT", "").strip().lower()
            if int(port) == 40000
            else ""
        ),
    }


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
