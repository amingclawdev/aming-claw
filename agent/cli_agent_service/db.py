"""Private SQLite storage for CLI Agent Service operational state."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5000


_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    profile_id TEXT PRIMARY KEY,
    profile_version TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    max_concurrency INTEGER NOT NULL CHECK (max_concurrency >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profile_states (
    profile_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN (
            'ready',
            'busy',
            'cooling_down',
            'quota_exhausted',
            'auth_required',
            'unhealthy',
            'disabled'
        )
    ),
    reason_code TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT NOT NULL DEFAULT '',
    quota_reset_at TEXT NOT NULL DEFAULT '',
    consecutive_crashes INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_crashes >= 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    run_json TEXT NOT NULL,
    state TEXT NOT NULL,
    parent_run_id TEXT NOT NULL DEFAULT '',
    successor_of_run_id TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    process_start_identity TEXT NOT NULL DEFAULT '',
    process_group_id INTEGER,
    argv_hash TEXT NOT NULL DEFAULT '',
    last_heartbeat_at TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    failure_category TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(profile_id)
);

CREATE TABLE IF NOT EXISTS agent_leases (
    lease_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL DEFAULT '',
    released_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
    FOREIGN KEY (profile_id) REFERENCES agent_profiles(profile_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_run
    ON agent_leases(run_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS active_profile_leases
    ON agent_leases(profile_id, status, expires_at);
CREATE INDEX IF NOT EXISTS run_successor_lineage
    ON agent_runs(successor_of_run_id);
CREATE INDEX IF NOT EXISTS profile_scheduler_state
    ON agent_profile_states(state, cooldown_until, quota_reset_at);
"""


def _tighten_file_mode(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def connect_registry_db(
    db_path: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open one configured connection to the dedicated host-private database."""
    path = Path(db_path).expanduser()
    if path.name == "governance.db":
        raise ValueError("CLI agent registry must not use the governance database")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    conn = sqlite3.connect(
        str(path),
        timeout=max(float(busy_timeout_ms) / 1000.0, 0.001),
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout={}".format(max(int(busy_timeout_ms), 0)))
    journal_mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    if journal_mode != "wal":
        conn.close()
        raise RuntimeError("CLI agent registry requires SQLite WAL mode")
    conn.execute("PRAGMA synchronous=NORMAL")
    _tighten_file_mode(path)
    _tighten_file_mode(Path(str(path) + "-wal"))
    _tighten_file_mode(Path(str(path) + "-shm"))
    return conn


def initialize_registry_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO registry_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO registry_meta(key, value) VALUES('scheduler_schema_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# Compact compatibility names for service callers.
connect = connect_registry_db
initialize = initialize_registry_db
open_registry_db = connect_registry_db
initialize_schema = initialize_registry_db
