"""Minimal executable runtime path for config-backed contracts.

This module intentionally stays independent from MCP and timeline facades. It
proves the new contract system can drive the next legal action and line-level
write authorization from source-controlled definitions before legacy
route-context migration begins. Live integrations should use the SQLite store
below so execution state has durable rows and compare-and-swap updates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from .execution_state import (
    _first_mapping_text,
    _line_instance_id_from_mapping,
    build_execution_state,
)
from .gate_decision import make_gate_decision
from .gate_kernel import ContractGateKernel
from .guide_compiler import attach_writer_role_safe_copy_payload, compile_runtime_guide
from .hash import canonical_json, stable_sha256
from .instructions import resolve_instruction_bundle
from .registry import ContractDefinitionRegistry
from .schema import ContractDefinitionError, is_new_execution_allowed, iter_stage_lines
from .write_gate import WriteGateDecision


log = logging.getLogger(__name__)
_JUDGMENT_HINTS_DISABLED_ENV = "AMING_JB_HINTS_DISABLED"
_JUDGMENT_HINT_PORT_ENV = "JUDGMENT_BRAIN_HINT_PORT"
_JUDGMENT_HINT_DEFAULT_PORT = "40123"
_JUDGMENT_HINT_TIMEOUT_SECONDS = 1.0


class ContractRuntimeError(ValueError):
    """Raised when a contract execution cannot be started or advanced."""


class StalePinnedContractExecutionError(ContractRuntimeError):
    """Raised when a persisted execution pins a stale source definition."""

    def __init__(
        self,
        field: str,
        expected: Any,
        actual: Any,
        *,
        record: Mapping[str, Any],
        definition: Mapping[str, Any] | None = None,
    ) -> None:
        self.field = str(field or "")
        self.expected = str(expected or "")
        self.actual = str(actual or "")
        self.record = deepcopy(dict(record))
        self.definition = deepcopy(dict(definition or {}))
        super().__init__(f"{self.field} mismatch for pinned contract execution")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "stale_pinned_contract_execution_error.v1",
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "contract_execution_id": str(
                self.record.get("contract_execution_id") or ""
            ),
            "contract_id": str(self.record.get("contract_id") or ""),
            "version": str(self.record.get("version") or ""),
            "revision": str(self.record.get("revision") or ""),
            "pinned_definition_hash": str(self.record.get("definition_hash") or ""),
            "current_definition_hash": str(
                self.definition.get("definition_hash") or ""
            ),
            "pinned_definition_source_sha256": str(
                self.record.get("definition_source_sha256") or ""
            ),
            "current_definition_source_sha256": str(
                self.definition.get("source_sha256") or ""
            ),
            "pinned_definition_governance_hints_sha256": str(
                self.record.get("definition_governance_hints_sha256") or ""
            ),
            "current_definition_governance_hints_sha256": str(
                self.definition.get("governance_hints_sha256") or ""
            ),
        }


LEGACY_PRIMARY_CONTRACT_ROUTE_IDS = frozenset(
    {
        "legacy_contract",
        "legacy_contract.v1",
        "legacy_contract_v1",
        "meta_contract",
        "meta_contract.v1",
        "meta_contract_v1",
        "meta_contract_gate",
        "task_timeline",
        "task_timeline_append",
        "timeline",
    }
)

LEGACY_PRIMARY_CONTRACT_ROUTE_SOURCES = frozenset(
    {
        "legacy_contract",
        "legacy_contract_route",
        "meta_contract",
        "meta_contract_gate",
        "task_timeline",
        "task_timeline_append",
        "timeline",
    }
)

LEGACY_CONTRACT_RECOVERY_ACTIONS = frozenset(
    {
        "audit_recovery",
        "read_historical_evidence",
        "record_friction_backlog",
        "task_timeline_append",
    }
)


def normalize_contract_route_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_")


def is_legacy_primary_contract_route(value: Any) -> bool:
    normalized = normalize_contract_route_token(value)
    return (
        normalized in LEGACY_PRIMARY_CONTRACT_ROUTE_IDS
        or normalized in LEGACY_PRIMARY_CONTRACT_ROUTE_SOURCES
    )


class InMemoryContractExecutionStore:
    """Small test/runtime store for contract executions.

    Live integrations use SQLiteContractExecutionStore. This in-memory adapter
    remains for pure unit tests and keeps the same CAS semantics.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        contract_execution_id = str(record.get("contract_execution_id") or "")
        if not contract_execution_id:
            raise ContractRuntimeError("contract_execution_id is required")
        if contract_execution_id in self._records:
            raise ContractRuntimeError(
                f"contract execution already exists: {contract_execution_id}"
            )
        stored = deepcopy(dict(record))
        self._records[contract_execution_id] = stored
        return deepcopy(stored)

    def get(self, contract_execution_id: str) -> dict[str, Any]:
        try:
            return deepcopy(self._records[contract_execution_id])
        except KeyError as exc:
            raise ContractRuntimeError(
                f"unknown contract execution: {contract_execution_id}"
            ) from exc

    def update(
        self,
        contract_execution_id: str,
        record: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if contract_execution_id not in self._records:
            raise ContractRuntimeError(
                f"unknown contract execution: {contract_execution_id}"
            )
        current_revision = int(
            self._records[contract_execution_id].get("execution_state_revision") or 0
        )
        if expected_revision is not None and current_revision != expected_revision:
            raise ContractRuntimeError("stale execution_state_revision")
        stored = deepcopy(dict(record))
        self._records[contract_execution_id] = stored
        return deepcopy(stored)


def _ensure_sqlite_schema_without_implicit_commit(
    conn: sqlite3.Connection,
    schema_sql: str,
    *,
    required_tables: Sequence[str],
) -> None:
    """Create schema without letting executescript split a caller transaction.

    sqlite3.Connection.executescript() commits an active transaction before it
    runs. ContractRuntime stores participate in larger facade transactions, so
    schema refresh during a write executes complete statements individually.
    SQLite DDL is then committed or rolled back with the paired Contract,
    runtime, and timeline evidence.
    """

    if not conn.in_transaction:
        conn.executescript(schema_sql)
        return

    statement = ""
    for line in schema_sql.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if sql:
            conn.execute(sql)
    if statement.strip():
        raise ContractRuntimeError("incomplete contract runtime schema statement")

    placeholders = ", ".join("?" for _ in required_tables)
    rows = conn.execute(
        f"""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name IN ({placeholders})
        """,
        tuple(required_tables),
    ).fetchall()
    present = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
        for row in rows
    }
    missing = sorted(set(required_tables) - present)
    if missing:
        raise ContractRuntimeError(
            "contract runtime schema must be initialized before transactional "
            f"writes; missing tables: {', '.join(missing)}"
        )


class SQLiteContractExecutionStore:
    """SQLite-backed contract execution store with CAS revision writes."""

    SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS contract_runtime_executions (
    contract_execution_id      TEXT PRIMARY KEY,
    project_id                 TEXT NOT NULL,
    backlog_id                 TEXT NOT NULL,
    contract_id                TEXT NOT NULL,
    version                    TEXT NOT NULL,
    revision                   TEXT NOT NULL,
    parent_contract_execution_id TEXT NOT NULL DEFAULT '',
    root_contract_execution_id TEXT NOT NULL DEFAULT '',
    contract_chain_id          TEXT NOT NULL DEFAULT '',
    execution_state_revision   INTEGER NOT NULL,
    record_json                TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contract_runtime_backlog
    ON contract_runtime_executions(project_id, backlog_id, contract_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_contract_runtime_chain
    ON contract_runtime_executions(contract_chain_id, parent_contract_execution_id);
"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        _ensure_sqlite_schema_without_implicit_commit(
            self.conn,
            self.SCHEMA_SQL,
            required_tables=("contract_runtime_executions",),
        )
        ensure_contract_chain_mapping_schema(self.conn)

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        contract_execution_id = str(record.get("contract_execution_id") or "")
        if not contract_execution_id:
            raise ContractRuntimeError("contract_execution_id is required")
        stored = deepcopy(dict(record))
        now = _utc_now()
        try:
            self.conn.execute(
                """
                INSERT INTO contract_runtime_executions (
                    contract_execution_id,
                    project_id,
                    backlog_id,
                    contract_id,
                    version,
                    revision,
                    parent_contract_execution_id,
                    root_contract_execution_id,
                    contract_chain_id,
                    execution_state_revision,
                    record_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_execution_id,
                    str(stored.get("project_id") or ""),
                    str(stored.get("backlog_id") or ""),
                    str(stored.get("contract_id") or ""),
                    str(stored.get("version") or ""),
                    str(stored.get("revision") or ""),
                    str(stored.get("parent_contract_execution_id") or ""),
                    str(stored.get("root_contract_execution_id") or ""),
                    str(stored.get("contract_chain_id") or ""),
                    int(stored.get("execution_state_revision") or 0),
                    _record_json(stored),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContractRuntimeError(
                f"contract execution already exists: {contract_execution_id}"
            ) from exc
        refresh_contract_chain_projection_for_record(self.conn, stored)
        return deepcopy(stored)

    def get(self, contract_execution_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT record_json FROM contract_runtime_executions
            WHERE contract_execution_id = ?
            """,
            (contract_execution_id,),
        ).fetchone()
        if row is None:
            raise ContractRuntimeError(
                f"unknown contract execution: {contract_execution_id}"
            )
        raw = row["record_json"] if isinstance(row, sqlite3.Row) else row[0]
        return _decode_record(raw)

    def update(
        self,
        contract_execution_id: str,
        record: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        stored = deepcopy(dict(record))
        params: list[Any] = [
            str(stored.get("project_id") or ""),
            str(stored.get("backlog_id") or ""),
            str(stored.get("contract_id") or ""),
            str(stored.get("version") or ""),
            str(stored.get("revision") or ""),
            str(stored.get("parent_contract_execution_id") or ""),
            str(stored.get("root_contract_execution_id") or ""),
            str(stored.get("contract_chain_id") or ""),
            int(stored.get("execution_state_revision") or 0),
            _record_json(stored),
            _utc_now(),
            contract_execution_id,
        ]
        where = "contract_execution_id = ?"
        if expected_revision is not None:
            where += " AND execution_state_revision = ?"
            params.append(expected_revision)
        cursor = self.conn.execute(
            f"""
            UPDATE contract_runtime_executions
            SET project_id = ?,
                backlog_id = ?,
                contract_id = ?,
                version = ?,
                revision = ?,
                parent_contract_execution_id = ?,
                root_contract_execution_id = ?,
                contract_chain_id = ?,
                execution_state_revision = ?,
                record_json = ?,
                updated_at = ?
            WHERE {where}
            """,
            tuple(params),
        )
        if cursor.rowcount == 0:
            if expected_revision is not None:
                raise ContractRuntimeError("stale execution_state_revision")
            raise ContractRuntimeError(
                f"unknown contract execution: {contract_execution_id}"
            )
        refresh_contract_chain_projection_for_record(self.conn, stored)
        return deepcopy(stored)

    def list_by_backlog(
        self,
        *,
        project_id: str,
        backlog_id: str,
        contract_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [project_id, backlog_id]
        where = "project_id = ? AND backlog_id = ?"
        if contract_id:
            where += " AND contract_id = ?"
            params.append(contract_id)
        rows = self.conn.execute(
            f"""
            SELECT record_json FROM contract_runtime_executions
            WHERE {where}
            ORDER BY updated_at DESC, contract_execution_id DESC
            """,
            tuple(params),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            raw = row["record_json"] if isinstance(row, sqlite3.Row) else row[0]
            records.append(_decode_record(raw))
        return records


CONTRACT_CHAIN_MAPPING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backlog_contract_chain_bindings (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key            TEXT NOT NULL UNIQUE,
    project_id                 TEXT NOT NULL,
    backlog_id                 TEXT NOT NULL,
    contract_chain_id          TEXT NOT NULL,
    root_contract_execution_id TEXT NOT NULL DEFAULT '',
    contract_execution_id      TEXT NOT NULL,
    parent_contract_execution_id TEXT NOT NULL DEFAULT '',
    contract_id                TEXT NOT NULL DEFAULT '',
    binding_kind               TEXT NOT NULL,
    generation                 INTEGER NOT NULL DEFAULT 0,
    execution_state_revision   INTEGER NOT NULL DEFAULT 0,
    source_ref                 TEXT NOT NULL DEFAULT '',
    source_hash                TEXT NOT NULL DEFAULT '',
    degraded_flags_json        TEXT NOT NULL DEFAULT '{}',
    metadata_json              TEXT NOT NULL DEFAULT '{}',
    created_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backlog_contract_chain_bindings_backlog
    ON backlog_contract_chain_bindings(project_id, backlog_id, id);
CREATE INDEX IF NOT EXISTS idx_backlog_contract_chain_bindings_execution
    ON backlog_contract_chain_bindings(contract_execution_id, id);
CREATE TABLE IF NOT EXISTS contract_chain_edges (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_key                   TEXT NOT NULL UNIQUE,
    project_id                 TEXT NOT NULL,
    backlog_id                 TEXT NOT NULL,
    contract_chain_id          TEXT NOT NULL,
    parent_contract_execution_id TEXT NOT NULL,
    child_contract_execution_id TEXT NOT NULL,
    root_contract_execution_id TEXT NOT NULL DEFAULT '',
    edge_kind                  TEXT NOT NULL,
    generation                 INTEGER NOT NULL DEFAULT 0,
    source_ref                 TEXT NOT NULL DEFAULT '',
    source_hash                TEXT NOT NULL DEFAULT '',
    metadata_json              TEXT NOT NULL DEFAULT '{}',
    created_at                 TEXT NOT NULL,
    UNIQUE (
        project_id,
        contract_chain_id,
        parent_contract_execution_id,
        child_contract_execution_id,
        edge_kind
    )
);
CREATE INDEX IF NOT EXISTS idx_contract_chain_edges_backlog
    ON contract_chain_edges(project_id, backlog_id, contract_chain_id, id);
CREATE TABLE IF NOT EXISTS backlog_contract_chain_current (
    project_id                 TEXT NOT NULL,
    backlog_id                 TEXT NOT NULL,
    contract_chain_id          TEXT NOT NULL DEFAULT '',
    root_contract_execution_id TEXT NOT NULL DEFAULT '',
    current_contract_execution_id TEXT NOT NULL DEFAULT '',
    current_contract_id        TEXT NOT NULL DEFAULT '',
    parent_to_resume_contract_execution_id TEXT NOT NULL DEFAULT '',
    active_child_contract_execution_id TEXT NOT NULL DEFAULT '',
    readiness_state            TEXT NOT NULL DEFAULT '',
    generation                 INTEGER NOT NULL DEFAULT 0,
    projection_watermark       INTEGER NOT NULL DEFAULT 0,
    projection_hash            TEXT NOT NULL DEFAULT '',
    active_chain_json          TEXT NOT NULL DEFAULT '{}',
    next_legal_action_json     TEXT NOT NULL DEFAULT '{}',
    degraded_flags_json        TEXT NOT NULL DEFAULT '{}',
    source_refs_json           TEXT NOT NULL DEFAULT '[]',
    updated_at                 TEXT NOT NULL,
    PRIMARY KEY (project_id, backlog_id)
);
CREATE INDEX IF NOT EXISTS idx_backlog_contract_chain_current_chain
    ON backlog_contract_chain_current(project_id, contract_chain_id);
"""


DIRECT_FIX_CONTRACT_IDS = frozenset({"direct_fix", "direct_fix.v1"})
MF_PARALLEL_CONTRACT_IDS = frozenset({"mf_parallel", "mf_parallel.v2", "mf_parallel.v1"})
DIRECT_FIX_QA_EVIDENCE_KINDS = frozenset(
    {"independent_verification", "direct_fix_independent_qa"}
)


def ensure_contract_chain_mapping_schema(conn: sqlite3.Connection) -> None:
    """Create durable backlog-to-contract-chain mapping tables."""

    _ensure_sqlite_schema_without_implicit_commit(
        conn,
        CONTRACT_CHAIN_MAPPING_SCHEMA_SQL,
        required_tables=(
            "backlog_contract_chain_bindings",
            "contract_chain_edges",
            "backlog_contract_chain_current",
        ),
    )


def refresh_contract_chain_projection_for_record(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    binding_kind: str = "",
    edge_kind: str = "",
) -> dict[str, Any]:
    """Upsert mapping rows for one execution and rebuild its backlog current view."""

    ensure_contract_chain_mapping_schema(conn)
    _upsert_contract_chain_binding(
        conn,
        record,
        binding_kind=binding_kind or _binding_kind_for_record(record),
    )
    if str(record.get("parent_contract_execution_id") or "").strip():
        _upsert_contract_chain_edge(
            conn,
            record,
            edge_kind=edge_kind or _edge_kind_for_record(record),
        )
    return rebuild_backlog_contract_chain_projection(
        conn,
        project_id=str(record.get("project_id") or ""),
        backlog_id=str(record.get("backlog_id") or ""),
    )


def upsert_contract_chain_root_current_binding(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    binding_kind: str = "root_current",
) -> dict[str, Any]:
    """Bind a root/service parent execution and refresh the current projection."""

    return refresh_contract_chain_projection_for_record(
        conn,
        record,
        binding_kind=binding_kind,
    )


def upsert_contract_chain_successor_binding(
    conn: sqlite3.Connection,
    *,
    parent_record: Mapping[str, Any],
    child_record: Mapping[str, Any],
    edge_kind: str = "",
    binding_kind: str = "successor_current",
) -> dict[str, Any]:
    """Bind a parent/child successor edge and refresh the current projection."""

    _validate_successor_lineage(parent_record, child_record)
    return refresh_contract_chain_projection_for_record(
        conn,
        child_record,
        binding_kind=binding_kind,
        edge_kind=edge_kind,
    )


def rebuild_backlog_contract_chain_projection(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    backlog_id: str,
) -> dict[str, Any]:
    """Rebuild the current projection from durable ContractRuntime executions."""

    project_id = str(project_id or "").strip()
    backlog_id = str(backlog_id or "").strip()
    if not project_id or not backlog_id:
        return {}
    ensure_contract_chain_mapping_schema(conn)
    rows = conn.execute(
        """
        SELECT record_json, created_at, updated_at
        FROM contract_runtime_executions
        WHERE project_id = ? AND backlog_id = ?
        ORDER BY created_at ASC, updated_at ASC, contract_execution_id ASC
        """,
        (project_id, backlog_id),
    ).fetchall()
    records: list[dict[str, Any]] = []
    row_times: dict[str, str] = {}
    for row in rows:
        raw = row["record_json"] if isinstance(row, sqlite3.Row) else row[0]
        record = _decode_record(raw)
        execution_id = str(record.get("contract_execution_id") or "")
        if not execution_id:
            continue
        records.append(record)
        row_times[execution_id] = (
            str(row["updated_at"] if isinstance(row, sqlite3.Row) else row[2])
        )
        _upsert_contract_chain_binding(
            conn,
            record,
            binding_kind=_binding_kind_for_record(record),
        )
        if str(record.get("parent_contract_execution_id") or "").strip():
            _upsert_contract_chain_edge(
                conn,
                record,
                edge_kind=_edge_kind_for_record(record),
            )

    if not records:
        conn.execute(
            """
            DELETE FROM backlog_contract_chain_current
            WHERE project_id = ? AND backlog_id = ?
            """,
            (project_id, backlog_id),
        )
        return {}

    chains: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        chain_id = str(record.get("contract_chain_id") or "").strip()
        if not chain_id:
            chain_id = str(record.get("root_contract_execution_id") or "").strip()
        if not chain_id:
            chain_id = str(record.get("contract_execution_id") or "").strip()
        chains.setdefault(chain_id, []).append(record)

    active_chain_id = _select_active_chain_id(chains, row_times)
    chain_records = chains.get(active_chain_id, [])
    degraded_flags: dict[str, Any] = {}
    if len(chains) > 1:
        degraded_flags["multi_active_chains"] = sorted(chains)
    if not active_chain_id:
        degraded_flags["missing_contract_chain_id"] = True

    root_record = _select_root_record(chain_records)
    current = _project_current_contract_state(
        chain_records,
        root_record=root_record,
        row_times=row_times,
    )
    watermark = _projection_watermark(conn, project_id, backlog_id)
    active_chain = {
        "schema_version": "backlog_contract_chain.active_chain.v1",
        "project_id": project_id,
        "backlog_id": backlog_id,
        "contract_chain_id": active_chain_id,
        "root_contract_execution_id": str(
            root_record.get("contract_execution_id") if root_record else ""
        ),
        "execution_count": len(chain_records),
        "execution_ids": [
            str(record.get("contract_execution_id") or "")
            for record in chain_records
            if str(record.get("contract_execution_id") or "")
        ],
    }
    source_refs = _projection_source_refs(chain_records)
    projection_row = {
        "schema_version": "backlog_contract_chain_current.v1",
        "project_id": project_id,
        "backlog_id": backlog_id,
        "contract_chain_id": active_chain_id,
        "root_contract_execution_id": active_chain["root_contract_execution_id"],
        "current_contract_execution_id": current["current_contract_execution_id"],
        "current_contract_id": current["current_contract_id"],
        "parent_to_resume_contract_execution_id": current[
            "parent_to_resume_contract_execution_id"
        ],
        "active_child_contract_execution_id": current[
            "active_child_contract_execution_id"
        ],
        "readiness_state": current["readiness_state"],
        "generation": current["generation"],
        "projection_watermark": watermark,
        "active_chain": active_chain,
        "next_legal_action": current["next_legal_action"],
        "degraded_flags": degraded_flags,
        "source_refs": source_refs,
        "source_of_proof": "contract_runtime_executions.completed_lines",
    }
    projection_hash = stable_sha256(_projection_hash_payload(projection_row))
    projection_row["projection_hash"] = projection_hash
    conn.execute(
        """
        INSERT INTO backlog_contract_chain_current (
            project_id,
            backlog_id,
            contract_chain_id,
            root_contract_execution_id,
            current_contract_execution_id,
            current_contract_id,
            parent_to_resume_contract_execution_id,
            active_child_contract_execution_id,
            readiness_state,
            generation,
            projection_watermark,
            projection_hash,
            active_chain_json,
            next_legal_action_json,
            degraded_flags_json,
            source_refs_json,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, backlog_id) DO UPDATE SET
            contract_chain_id = excluded.contract_chain_id,
            root_contract_execution_id = excluded.root_contract_execution_id,
            current_contract_execution_id = excluded.current_contract_execution_id,
            current_contract_id = excluded.current_contract_id,
            parent_to_resume_contract_execution_id = excluded.parent_to_resume_contract_execution_id,
            active_child_contract_execution_id = excluded.active_child_contract_execution_id,
            readiness_state = excluded.readiness_state,
            generation = excluded.generation,
            projection_watermark = excluded.projection_watermark,
            projection_hash = excluded.projection_hash,
            active_chain_json = excluded.active_chain_json,
            next_legal_action_json = excluded.next_legal_action_json,
            degraded_flags_json = excluded.degraded_flags_json,
            source_refs_json = excluded.source_refs_json,
            updated_at = excluded.updated_at
        """,
        (
            project_id,
            backlog_id,
            active_chain_id,
            projection_row["root_contract_execution_id"],
            projection_row["current_contract_execution_id"],
            projection_row["current_contract_id"],
            projection_row["parent_to_resume_contract_execution_id"],
            projection_row["active_child_contract_execution_id"],
            projection_row["readiness_state"],
            projection_row["generation"],
            watermark,
            projection_hash,
            _record_json(active_chain),
            _record_json(current["next_legal_action"]),
            _record_json(degraded_flags),
            json.dumps(source_refs, sort_keys=True, separators=(",", ":")),
            _utc_now(),
        ),
    )
    return read_backlog_contract_chain_current(
        conn,
        project_id=project_id,
        backlog_id=backlog_id,
    )


def read_backlog_contract_chain_current(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    backlog_id: str,
    rebuild_if_missing: bool = False,
) -> dict[str, Any]:
    """Read the onboard/runtime current projection for a backlog row."""

    project_id = str(project_id or "").strip()
    backlog_id = str(backlog_id or "").strip()
    if not project_id or not backlog_id:
        return {}
    ensure_contract_chain_mapping_schema(conn)
    row = conn.execute(
        """
        SELECT *
        FROM backlog_contract_chain_current
        WHERE project_id = ? AND backlog_id = ?
        """,
        (project_id, backlog_id),
    ).fetchone()
    if row is None and rebuild_if_missing:
        return rebuild_backlog_contract_chain_projection(
            conn,
            project_id=project_id,
            backlog_id=backlog_id,
        )
    if row is None:
        return {}
    return _current_projection_from_row(row)


def _upsert_contract_chain_binding(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    binding_kind: str,
) -> None:
    project_id = str(record.get("project_id") or "").strip()
    backlog_id = str(record.get("backlog_id") or "").strip()
    execution_id = str(record.get("contract_execution_id") or "").strip()
    if not project_id or not backlog_id or not execution_id:
        return
    revision = int(record.get("execution_state_revision") or 0)
    source_hash = stable_sha256(_binding_hash_payload(record))
    idempotency_key = stable_sha256(
        {
            "kind": binding_kind,
            "project_id": project_id,
            "backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            "execution_state_revision": revision,
            "source_hash": source_hash,
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO backlog_contract_chain_bindings (
            idempotency_key,
            project_id,
            backlog_id,
            contract_chain_id,
            root_contract_execution_id,
            contract_execution_id,
            parent_contract_execution_id,
            contract_id,
            binding_kind,
            generation,
            execution_state_revision,
            source_ref,
            source_hash,
            degraded_flags_json,
            metadata_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            idempotency_key,
            project_id,
            backlog_id,
            str(record.get("contract_chain_id") or ""),
            str(record.get("root_contract_execution_id") or ""),
            execution_id,
            str(record.get("parent_contract_execution_id") or ""),
            str(record.get("contract_id") or ""),
            binding_kind,
            revision,
            revision,
            f"contract_runtime:{execution_id}:revision:{revision}",
            source_hash,
            "{}",
            _record_json(_binding_metadata(record)),
            _utc_now(),
        ),
    )


def _upsert_contract_chain_edge(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    edge_kind: str,
) -> None:
    project_id = str(record.get("project_id") or "").strip()
    backlog_id = str(record.get("backlog_id") or "").strip()
    parent_id = str(record.get("parent_contract_execution_id") or "").strip()
    child_id = str(record.get("contract_execution_id") or "").strip()
    chain_id = str(record.get("contract_chain_id") or "").strip()
    if not project_id or not backlog_id or not parent_id or not child_id:
        return
    source_hash = stable_sha256(
        {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "contract_chain_id": chain_id,
            "parent_contract_execution_id": parent_id,
            "child_contract_execution_id": child_id,
            "edge_kind": edge_kind,
        }
    )
    edge_key = stable_sha256(
        {
            "project_id": project_id,
            "contract_chain_id": chain_id,
            "parent_contract_execution_id": parent_id,
            "child_contract_execution_id": child_id,
            "edge_kind": edge_kind,
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO contract_chain_edges (
            edge_key,
            project_id,
            backlog_id,
            contract_chain_id,
            parent_contract_execution_id,
            child_contract_execution_id,
            root_contract_execution_id,
            edge_kind,
            generation,
            source_ref,
            source_hash,
            metadata_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge_key,
            project_id,
            backlog_id,
            chain_id,
            parent_id,
            child_id,
            str(record.get("root_contract_execution_id") or ""),
            edge_kind,
            int(record.get("execution_state_revision") or 0),
            f"contract_runtime:{child_id}",
            source_hash,
            _record_json(_binding_metadata(record)),
            _utc_now(),
        ),
    )


def _validate_successor_lineage(
    parent_record: Mapping[str, Any],
    child_record: Mapping[str, Any],
) -> None:
    parent_execution_id = str(
        parent_record.get("contract_execution_id") or ""
    ).strip()
    child_execution_id = str(child_record.get("contract_execution_id") or "").strip()
    if not parent_execution_id or not child_execution_id:
        raise ContractRuntimeError("successor binding requires parent and child executions")
    child_parent_id = str(
        child_record.get("parent_contract_execution_id") or ""
    ).strip()
    if child_parent_id != parent_execution_id:
        raise ContractRuntimeError(
            "successor child parent_contract_execution_id does not match parent_record"
        )
    expected_root_id = str(
        parent_record.get("root_contract_execution_id") or parent_execution_id
    ).strip()
    child_root_id = str(
        child_record.get("root_contract_execution_id") or ""
    ).strip()
    if child_root_id != expected_root_id:
        raise ContractRuntimeError(
            "successor child root_contract_execution_id does not match parent lineage"
        )
    expected_chain_id = str(parent_record.get("contract_chain_id") or "").strip()
    child_chain_id = str(child_record.get("contract_chain_id") or "").strip()
    if expected_chain_id and child_chain_id != expected_chain_id:
        raise ContractRuntimeError(
            "successor child contract_chain_id does not match parent lineage"
        )
    parent_project_id = str(parent_record.get("project_id") or "").strip()
    child_project_id = str(child_record.get("project_id") or "").strip()
    if parent_project_id and child_project_id != parent_project_id:
        raise ContractRuntimeError(
            "successor child project_id does not match parent_record"
        )
    parent_backlog_id = str(parent_record.get("backlog_id") or "").strip()
    child_backlog_id = str(child_record.get("backlog_id") or "").strip()
    if parent_backlog_id and child_backlog_id != parent_backlog_id:
        raise ContractRuntimeError(
            "successor child backlog_id does not match parent_record"
        )


def _binding_kind_for_record(record: Mapping[str, Any]) -> str:
    parent_id = str(record.get("parent_contract_execution_id") or "").strip()
    contract_id = str(record.get("contract_id") or "").strip()
    if not parent_id and contract_id == "onboard_route_guide":
        return "onboard_service_root_current"
    if not parent_id:
        return "root_current"
    if contract_id in DIRECT_FIX_CONTRACT_IDS:
        return "direct_fix_child_current"
    if contract_id in MF_PARALLEL_CONTRACT_IDS:
        return "mf_parallel_child_current"
    return "successor_current"


def _edge_kind_for_record(record: Mapping[str, Any]) -> str:
    contract_id = str(record.get("contract_id") or "").strip()
    if contract_id in DIRECT_FIX_CONTRACT_IDS:
        return "direct_fix_child"
    if contract_id in MF_PARALLEL_CONTRACT_IDS:
        return "mf_parallel_child"
    if contract_id:
        return f"{contract_id}_child"
    return "successor_child"


def _binding_hash_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project_id": str(record.get("project_id") or ""),
        "backlog_id": str(record.get("backlog_id") or ""),
        "contract_execution_id": str(record.get("contract_execution_id") or ""),
        "contract_id": str(record.get("contract_id") or ""),
        "contract_chain_id": str(record.get("contract_chain_id") or ""),
        "parent_contract_execution_id": str(
            record.get("parent_contract_execution_id") or ""
        ),
        "root_contract_execution_id": str(
            record.get("root_contract_execution_id") or ""
        ),
        "execution_state_revision": int(record.get("execution_state_revision") or 0),
        "completed_line_count": len(record.get("completed_lines") or []),
    }


def _binding_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "route_token_ref_present": bool(str(record.get("route_token_ref") or "")),
        "metadata": dict(record.get("metadata") or {})
        if isinstance(record.get("metadata"), Mapping)
        else {},
        "backlog_lineage": dict(record.get("backlog_lineage") or {})
        if isinstance(record.get("backlog_lineage"), Mapping)
        else {},
    }


def _projection_watermark(
    conn: sqlite3.Connection,
    project_id: str,
    backlog_id: str,
) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(id), 0)
        FROM backlog_contract_chain_bindings
        WHERE project_id = ? AND backlog_id = ?
        """,
        (project_id, backlog_id),
    ).fetchone()
    if row is None:
        return 0
    return int(row[0] if not isinstance(row, sqlite3.Row) else row[0] or 0)


def _projection_hash_payload(projection_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in projection_row.items()
        if str(key) not in {"projection_hash", "projection_watermark", "updated_at"}
    }


def _select_active_chain_id(
    chains: Mapping[str, list[dict[str, Any]]],
    row_times: Mapping[str, str],
) -> str:
    best_id = ""
    best_key: tuple[str, int, str] = ("", -1, "")
    for chain_id, records in chains.items():
        latest_time = ""
        latest_revision = 0
        latest_execution = ""
        for record in records:
            execution_id = str(record.get("contract_execution_id") or "")
            latest_time = max(latest_time, str(row_times.get(execution_id) or ""))
            latest_revision = max(
                latest_revision,
                int(record.get("execution_state_revision") or 0),
            )
            latest_execution = max(latest_execution, execution_id)
        key = (latest_time, latest_revision, latest_execution)
        if key > best_key:
            best_key = key
            best_id = chain_id
    return best_id


def _select_root_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in records:
        execution_id = str(record.get("contract_execution_id") or "")
        parent_id = str(record.get("parent_contract_execution_id") or "")
        root_id = str(record.get("root_contract_execution_id") or "")
        if not parent_id and (not root_id or root_id == execution_id):
            return record
    return records[0] if records else {}


def _project_current_contract_state(
    records: list[dict[str, Any]],
    *,
    root_record: Mapping[str, Any],
    row_times: Mapping[str, str],
) -> dict[str, Any]:
    if not records:
        return _empty_projected_state("missing_contract_runtime_execution")
    direct_fix_records = sorted(
        (
            record
            for record in records
            if _record_contract_id(record) in DIRECT_FIX_CONTRACT_IDS
        ),
        key=lambda record: _record_order_key(record, row_times=row_times),
    )
    direct_fix = direct_fix_records[-1] if direct_fix_records else {}
    if direct_fix:
        direct_state = _project_direct_fix_state(direct_fix, root_record=root_record)
        if direct_state:
            if direct_state.get("readiness_state") == (
                "parent_resume_required_after_direct_fix_qa"
            ):
                later_successor = _latest_record(
                    [
                        record
                        for record in records
                        if _record_contract_id(record) not in DIRECT_FIX_CONTRACT_IDS
                        and str(record.get("parent_contract_execution_id") or "").strip()
                        and _record_order_key(record, row_times=row_times)
                        > _record_order_key(direct_fix, row_times=row_times)
                    ],
                    row_times=row_times,
                )
                if later_successor:
                    return _project_record_state(later_successor)
                for returned_child in direct_fix_records:
                    returned_state = _project_direct_fix_state(
                        returned_child,
                        root_record=root_record,
                    )
                    if returned_state.get("readiness_state") != (
                        "parent_resume_required_after_direct_fix_qa"
                    ):
                        continue
                    if not _parent_resume_acknowledged(root_record, returned_child):
                        return returned_state
                return _project_record_state(root_record)
            return direct_state
    incomplete = [
        record
        for record in records
        if not _record_is_complete(record)
    ]
    incomplete_children = [
        record
        for record in incomplete
        if str(record.get("parent_contract_execution_id") or "").strip()
    ]
    current_record = _latest_record(
        incomplete_children or incomplete or records,
        row_times=row_times,
    )
    if not current_record:
        return _empty_projected_state("missing_contract_runtime_execution")
    return _project_record_state(current_record)


def _project_record_state(record: Mapping[str, Any]) -> dict[str, Any]:
    current_id = str(record.get("contract_execution_id") or "")
    current_contract_id = _record_contract_id(record)
    next_action = _next_action_from_record(record)
    readiness = "contract_complete" if _record_is_complete(record) else "contract_active"
    return {
        "current_contract_execution_id": current_id,
        "current_contract_id": current_contract_id,
        "parent_to_resume_contract_execution_id": "",
        "active_child_contract_execution_id": (
            current_id if str(record.get("parent_contract_execution_id") or "") else ""
        ),
        "readiness_state": readiness,
        "generation": int(record.get("execution_state_revision") or 0),
        "next_legal_action": next_action,
    }


def _project_direct_fix_state(
    child: Mapping[str, Any],
    *,
    root_record: Mapping[str, Any],
) -> dict[str, Any]:
    child_id = str(child.get("contract_execution_id") or "")
    parent_id = str(child.get("parent_contract_execution_id") or "")
    generation = int(child.get("execution_state_revision") or 0)
    if not child_id:
        return {}
    repair_line = _find_direct_fix_repair_line(child)
    qa_graph_line = _find_direct_fix_qa_graph_line(child, repair_line=repair_line)
    return_line = _find_completed_line(
        child,
        line_ids={"direct_fix_return_to_parent"},
        evidence_kinds={"direct_fix_return_to_parent"},
    )
    qa_line = _find_direct_fix_qa_line(
        child,
        generation=generation,
        repair_line=repair_line,
        qa_graph_line=qa_graph_line,
    )
    if return_line and qa_line and _direct_fix_return_follows_qa(
        return_line,
        qa_line=qa_line,
    ):
        return {
            "current_contract_execution_id": parent_id
            or str(root_record.get("contract_execution_id") or ""),
            "current_contract_id": _record_contract_id(root_record),
            "parent_to_resume_contract_execution_id": parent_id,
            "active_child_contract_execution_id": "",
            "readiness_state": "parent_resume_required_after_direct_fix_qa",
            "generation": generation,
            "next_legal_action": _parent_resume_next_action(
                child,
                parent_id=parent_id,
                generation=generation,
                return_line=return_line,
            ),
        }
    if qa_line:
        return {
            "current_contract_execution_id": child_id,
            "current_contract_id": _record_contract_id(child),
            "parent_to_resume_contract_execution_id": parent_id,
            "active_child_contract_execution_id": child_id,
            "readiness_state": "return_to_parent_after_direct_fix_qa",
            "generation": generation,
            "next_legal_action": _direct_fix_return_next_action(
                child,
                parent_id=parent_id,
                generation=generation,
                qa_line=qa_line,
            ),
        }
    if repair_line:
        if _direct_fix_graph_gates_active(child) and not qa_graph_line:
            return {
                "current_contract_execution_id": child_id,
                "current_contract_id": _record_contract_id(child),
                "parent_to_resume_contract_execution_id": parent_id,
                "active_child_contract_execution_id": child_id,
                "readiness_state": "direct_fix_complete_awaiting_independent_qa_graph",
                "generation": generation,
                "next_legal_action": _direct_fix_qa_graph_next_action(
                    child,
                    generation=generation,
                    repair_line=repair_line,
                ),
            }
        return {
            "current_contract_execution_id": child_id,
            "current_contract_id": _record_contract_id(child),
            "parent_to_resume_contract_execution_id": parent_id,
            "active_child_contract_execution_id": child_id,
            "readiness_state": "direct_fix_complete_awaiting_independent_qa",
            "generation": generation,
            "next_legal_action": _direct_fix_qa_next_action(
                child,
                generation=generation,
                repair_line=repair_line,
                qa_graph_line=qa_graph_line,
            ),
        }
    return {
        "current_contract_execution_id": child_id,
        "current_contract_id": _record_contract_id(child),
        "parent_to_resume_contract_execution_id": parent_id,
        "active_child_contract_execution_id": child_id,
        "readiness_state": "direct_fix_child_active",
        "generation": generation,
        "next_legal_action": _next_action_from_record(child),
    }


def _empty_projected_state(readiness_state: str) -> dict[str, Any]:
    return {
        "current_contract_execution_id": "",
        "current_contract_id": "",
        "parent_to_resume_contract_execution_id": "",
        "active_child_contract_execution_id": "",
        "readiness_state": readiness_state,
        "generation": 0,
        "next_legal_action": {},
    }


def _latest_record(
    records: list[dict[str, Any]],
    *,
    row_times: Mapping[str, str],
) -> dict[str, Any]:
    if not records:
        return {}
    return max(
        records,
        key=lambda record: _record_order_key(record, row_times=row_times),
    )


def _record_order_key(
    record: Mapping[str, Any],
    *,
    row_times: Mapping[str, str],
) -> tuple[str, str, int]:
    execution_id = str(record.get("contract_execution_id") or "")
    return (
        str(row_times.get(execution_id) or ""),
        execution_id,
        int(record.get("execution_state_revision") or 0),
    )


def _record_contract_id(record: Mapping[str, Any]) -> str:
    return str(record.get("contract_id") or "").strip()


_DIRECT_FIX_GRAPH_GATE_LINE_IDS = frozenset(
    {
        "direct_fix_observer_graph_scope",
        "direct_fix_worker_graph_context",
        "direct_fix_qa_graph_context",
    }
)
_DIRECT_FIX_REPAIR_QA_GRAPH_GATE_LINE_IDS = frozenset(
    {
        "direct_fix_worker_graph_context",
        "direct_fix_qa_graph_context",
    }
)


def _direct_fix_graph_gates_active(record: Mapping[str, Any]) -> bool:
    features = (
        record.get("contract_runtime_features")
        if isinstance(record.get("contract_runtime_features"), Mapping)
        else {}
    )
    if "direct_fix_graph_query_gate" in features:
        return bool(features.get("direct_fix_graph_query_gate"))
    for line in record.get("completed_lines") or []:
        if (
            isinstance(line, Mapping)
            and str(line.get("line_id") or "") in _DIRECT_FIX_GRAPH_GATE_LINE_IDS
        ):
            return True
    guide = record.get("runtime_guide") if isinstance(record.get("runtime_guide"), Mapping) else {}
    next_line = guide.get("next_legal_action") if isinstance(guide.get("next_legal_action"), Mapping) else {}
    return str(next_line.get("line_id") or "") in _DIRECT_FIX_GRAPH_GATE_LINE_IDS


def _contract_runtime_features(definition: Mapping[str, Any]) -> dict[str, Any]:
    line_ids = {
        str(line.get("line_id") or "")
        for _, line in iter_stage_lines(definition)
    }
    return {
        "schema_version": "contract_runtime_features.v1",
        "direct_fix_graph_query_gate": (
            _DIRECT_FIX_REPAIR_QA_GRAPH_GATE_LINE_IDS.issubset(line_ids)
        ),
    }


def _contract_runtime_features_for_record(
    record: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    features = record.get("contract_runtime_features")
    if isinstance(features, Mapping):
        return dict(features)
    return _contract_runtime_features(definition)


_LEGACY_WORKER_GRAPH_CONTEXT_COMPAT_ERRORS = {
    "worker_graph_context requires non-empty graph_trace_ids",
    "worker_graph_context requires db_verified graph_trace_evidence",
    "worker_graph_context requires graph query_source",
    "worker_graph_context requires graph query_purpose",
    "worker_graph_context requires worker_role=mf_sub",
    "worker_graph_context requires runtime_context_id",
    "worker_graph_context requires task_id",
    "worker_graph_context requires parent_task_id",
    "worker_graph_context requires target_project_root",
}


def _direct_fix_worker_graph_context_compat_decision(
    *,
    definition: Mapping[str, Any],
    record: Mapping[str, Any],
    write: Mapping[str, Any],
    gate_decision: Any,
) -> Any | None:
    if getattr(gate_decision, "ok", False):
        return None
    if str(write.get("line_id") or "").strip() != "worker_graph_context":
        return None
    if _record_contract_id(record) not in {"mf_parallel", "mf_parallel.v2"}:
        return None
    if str(write.get("actor_role") or "").strip() != "mf_sub":
        return None
    features = _contract_runtime_features_for_record(record, definition)
    if features.get("direct_fix_graph_query_gate") is not False:
        return None
    errors = tuple(str(item) for item in getattr(gate_decision, "errors", ()) if str(item))
    if not errors or not set(errors).issubset(_LEGACY_WORKER_GRAPH_CONTEXT_COMPAT_ERRORS):
        return None
    imported_checks: list[dict[str, Any]] = []
    for item in getattr(gate_decision, "imported_legacy_checks", ()) or ():
        imported = dict(item)
        if str(imported.get("adapter_id") or "") == "contract_write_gate.v1":
            imported["decision"] = "warn"
            imported["ok"] = True
            imported["errors"] = []
            imported["warnings"] = [
                *list(imported.get("warnings") or []),
                "legacy_worker_graph_context_graph_gate_disabled",
            ]
            imported["legacy_authoritative"] = False
        imported_checks.append(imported)
    return make_gate_decision(
        action=str(getattr(gate_decision, "action", "") or "submit_line"),
        gate_id=str(getattr(gate_decision, "gate_id", "") or ""),
        warnings=(
            *tuple(str(item) for item in getattr(gate_decision, "warnings", ()) if str(item)),
            "legacy_worker_graph_context_graph_gate_disabled",
        ),
        next_move=getattr(gate_decision, "next_move", {}) or {},
        gate_type=str(getattr(gate_decision, "gate_type", "") or ""),
        stage_id=str(getattr(gate_decision, "stage_id", "") or ""),
        line_id=str(getattr(gate_decision, "line_id", "") or ""),
        required_role=str(getattr(gate_decision, "required_role", "") or ""),
        actor_role=str(getattr(gate_decision, "actor_role", "") or ""),
        hash_status=getattr(gate_decision, "hash_status", {}) or {},
        graph_status=getattr(gate_decision, "graph_status", {}) or {},
        dirty_scope_status=getattr(gate_decision, "dirty_scope_status", {}) or {},
        imported_legacy_checks=tuple(imported_checks),
        projection_actions=getattr(gate_decision, "projection_actions", ()) or (),
        policy_hash=str(getattr(gate_decision, "policy_hash", "") or ""),
        contract_definition_hash=str(
            getattr(gate_decision, "contract_definition_hash", "") or ""
        ),
        execution_state_revision=int(
            getattr(gate_decision, "execution_state_revision", 0) or 0
        ),
        runtime_guide_hash=str(getattr(gate_decision, "runtime_guide_hash", "") or ""),
    )


def _record_is_complete(record: Mapping[str, Any]) -> bool:
    guide = (
        record.get("runtime_guide")
        if isinstance(record.get("runtime_guide"), Mapping)
        else {}
    )
    return guide.get("next_legal_action") is None


def _next_action_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    guide = (
        record.get("runtime_guide")
        if isinstance(record.get("runtime_guide"), Mapping)
        else {}
    )
    next_line = guide.get("next_legal_action")
    if not isinstance(next_line, Mapping):
        return {}
    evidence_kind = str(next_line.get("evidence_kind") or "")
    line_id = str(next_line.get("line_id") or "")
    return {
        "schema_version": "backlog_contract_chain.next_action.v1",
        "id": line_id,
        "action": str(next_line.get("action") or "").strip()
        or (f"record_{evidence_kind}" if evidence_kind else "record_contract_line"),
        "source": "backlog_contract_chain_current",
        "precedence": "contract_runtime_first_missing_line",
        "contract_execution_id": str(record.get("contract_execution_id") or ""),
        "parent_contract_execution_id": str(
            record.get("parent_contract_execution_id") or ""
        ),
        "root_contract_execution_id": str(record.get("root_contract_execution_id") or ""),
        "contract_chain_id": str(record.get("contract_chain_id") or ""),
        "contract_id": _record_contract_id(record),
        "stage_id": str(next_line.get("stage_id") or ""),
        "line_id": line_id,
        "owner_role": str(next_line.get("owner_role") or ""),
        "allowed_writer_roles": list(next_line.get("allowed_writer_roles") or []),
        "evidence_kind": evidence_kind,
        "execution_state_revision": int(record.get("execution_state_revision") or 0),
        "route_token_ref": str(record.get("route_token_ref") or ""),
        "meta_contract_gate_decision_source": False,
    }


def _find_completed_line(
    record: Mapping[str, Any],
    *,
    line_ids: set[str],
    evidence_kinds: set[str],
    after_index: int = -1,
) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    for index, line in reversed(list(enumerate(lines))):
        if index <= after_index:
            continue
        if not isinstance(line, Mapping):
            continue
        line_id = str(line.get("line_id") or "")
        evidence_kind = str(line.get("evidence_kind") or "")
        if line_id in line_ids or evidence_kind in evidence_kinds:
            enriched = dict(line)
            enriched["_completed_line_index"] = index
            return enriched
    return {}


def _find_direct_fix_repair_line(record: Mapping[str, Any]) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    after_index = _last_failed_qa_line_index(lines)
    if _direct_fix_graph_gates_active(record):
        worker_graph = _find_completed_line(
            record,
            line_ids={"direct_fix_worker_graph_context"},
            evidence_kinds=set(),
            after_index=after_index,
        )
        if not worker_graph:
            return {}
        after_index = max(after_index, _completed_line_index(worker_graph))
    return _find_completed_line(
        record,
        line_ids={"direct_fix_candidate_repair"},
        evidence_kinds={"direct_fix_repair_evidence"},
        after_index=after_index,
    )


def _find_direct_fix_qa_graph_line(
    record: Mapping[str, Any],
    *,
    repair_line: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not _direct_fix_graph_gates_active(record):
        return {}
    repair_index = _completed_line_index(repair_line or {})
    if repair_index < 0:
        return {}
    return _find_completed_line(
        record,
        line_ids={"direct_fix_qa_graph_context"},
        evidence_kinds=set(),
        after_index=repair_index,
    )


def _find_direct_fix_qa_line(
    record: Mapping[str, Any],
    *,
    generation: int,
    repair_line: Mapping[str, Any] | None = None,
    qa_graph_line: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    execution_id = str(record.get("contract_execution_id") or "")
    repair_index = _completed_line_index(repair_line or {})
    required_after_index = repair_index
    if _direct_fix_graph_gates_active(record):
        qa_graph_index = _completed_line_index(qa_graph_line or {})
        if qa_graph_index < 0:
            return {}
        required_after_index = max(required_after_index, qa_graph_index)
    for index, line in reversed(list(enumerate(lines))):
        if not isinstance(line, Mapping):
            continue
        if str(line.get("actor_role") or "").strip() != "qa":
            continue
        if str(line.get("evidence_kind") or "").strip() not in DIRECT_FIX_QA_EVIDENCE_KINDS:
            continue
        if not _line_status_allows_direct_fix_qa(line):
            continue
        explicit_scope_matches = _line_scope_matches_direct_fix_child(
            line,
            contract_execution_id=execution_id,
            generation=generation,
        )
        if not explicit_scope_matches and not _line_is_post_repair_child_qa(
            index,
            repair_index=required_after_index,
        ):
            continue
        enriched = dict(line)
        enriched["_completed_line_index"] = index
        enriched["_source_ref"] = f"contract_runtime:{execution_id}:completed_lines:{index}"
        return enriched
    return {}


def _completed_line_index(line: Mapping[str, Any]) -> int:
    try:
        return int(line.get("_completed_line_index"))
    except (TypeError, ValueError):
        return -1


def _line_is_post_repair_child_qa(
    index: int,
    *,
    repair_index: int,
) -> bool:
    if repair_index < 0:
        return False
    return index > repair_index


def _direct_fix_return_follows_qa(
    return_line: Mapping[str, Any],
    *,
    qa_line: Mapping[str, Any],
) -> bool:
    return_index = _completed_line_index(return_line)
    qa_index = _completed_line_index(qa_line)
    if return_index < 0 or qa_index < 0:
        return False
    return return_index > qa_index


def _parent_resume_acknowledged(
    parent: Mapping[str, Any],
    child: Mapping[str, Any],
) -> bool:
    child_id = str(child.get("contract_execution_id") or "").strip()
    parent_id = str(parent.get("contract_execution_id") or "").strip()
    if not child_id:
        return False
    line = _find_parent_resume_ack_line(
        parent,
        parent_id=parent_id,
        child_id=child_id,
    )
    if not line:
        return False
    return True


def _find_parent_resume_ack_line(
    record: Mapping[str, Any],
    *,
    parent_id: str = "",
    child_id: str = "",
) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    for index, line in reversed(list(enumerate(lines))):
        if not isinstance(line, Mapping):
            continue
        if not _parent_resume_ack_line_matches(
            line,
            parent_id=parent_id,
            child_id=child_id,
        ):
            continue
        enriched = dict(line)
        enriched["_completed_line_index"] = index
        return enriched
    return {}


def _parent_resume_ack_line_matches(
    line: Mapping[str, Any],
    *,
    parent_id: str = "",
    child_id: str = "",
) -> bool:
    if str(line.get("line_id") or "") != "resume_parent_after_successor_return":
        return False
    if str(line.get("evidence_kind") or "") != "successor_return_acknowledgement":
        return False
    if str(line.get("actor_role") or "").strip() != "observer":
        return False
    if not _line_status_allows_direct_fix_qa(line):
        return False
    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    if (
        parent_id
        and str(payload.get("parent_contract_execution_id") or "").strip() != parent_id
    ):
        return False
    successor_contract_id = str(payload.get("successor_contract_id") or "").strip()
    if successor_contract_id not in DIRECT_FIX_CONTRACT_IDS:
        return False
    successor_execution_id = str(
        payload.get("successor_contract_execution_id") or ""
    ).strip()
    if child_id and successor_execution_id != child_id:
        return False
    return True


def _line_status_allows_direct_fix_qa(line: Mapping[str, Any]) -> bool:
    return _line_status_allows_contract_completion(line)


_CONTRACT_COMPLETION_BLOCKING_STATUSES = frozenset(
    {"fail", "failed", "failure", "rejected", "blocked"}
)
_CONTRACT_COMPLETION_STATUS_FIELDS = frozenset(
    {
        "status",
        "verdict",
        "outcome",
        "decision",
        "result",
        "qa_status",
        "qa_decision",
        "verification_status",
        "verification_decision",
    }
)
_CONTRACT_COMPLETION_FAILURE_COUNT_FIELDS = frozenset(
    {"failed", "failures", "failed_count", "failure_count", "error_count"}
)


def _contract_completion_satisfying_lines(
    lines: Sequence[Mapping[str, Any]],
    *,
    failed_qa_rejoin_contexts: set[tuple[str, str]] | None = None,
    failed_qa_rejoin_markers: Sequence[Mapping[str, Any]] | None = None,
) -> list[Mapping[str, Any]]:
    last_failed_qa_index = _last_failed_qa_line_index(lines)
    post_failed_retry_contexts = _post_failed_qa_retry_context_keys(
        lines,
        failed_qa_index=last_failed_qa_index,
    )
    post_failed_retry_contexts.update(failed_qa_rejoin_contexts or set())
    failed_qa_rejoin_boundaries = _failed_qa_rejoin_boundaries(
        lines,
        failed_qa_rejoin_markers or [],
    )
    satisfying: list[Mapping[str, Any]] = []
    for index, line in enumerate(lines):
        if isinstance(line, Mapping) and not _line_shape_allows_contract_completion(
            line
        ):
            continue
        if (
            last_failed_qa_index < 0
            and failed_qa_rejoin_markers
            and str(line.get("line_id") or "").strip()
            in _FAILED_QA_RETRY_PROOF_LINE_IDS
            and _line_retry_context_keys(line).intersection(
                failed_qa_rejoin_contexts or set()
            )
            and not _line_survives_failed_qa_rejoin_boundary(
                line,
                index=index,
                boundaries=failed_qa_rejoin_boundaries,
            )
        ):
            continue
        if last_failed_qa_index >= 0 and index <= last_failed_qa_index:
            if not _line_survives_failed_qa_retry_reset(
                line,
                post_failed_retry_contexts=post_failed_retry_contexts,
            ):
                continue
        if isinstance(line, Mapping) and not _line_status_allows_contract_completion(
            line
        ):
            continue
        satisfying.append(line)
    return satisfying


def _line_id_present(
    lines: Sequence[Mapping[str, Any]],
    line_id: str,
) -> bool:
    return any(
        isinstance(line, Mapping)
        and str(line.get("line_id") or "").strip() == line_id
        for line in lines
    )


def _first_deep_contract_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value.get(key)
        for item in value.values():
            found = _first_deep_contract_value(item, key)
            if found not in (None, ""):
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found = _first_deep_contract_value(item, key)
            if found not in (None, ""):
                return found
    return None


def _contract_graph_line_db_verified(line: Mapping[str, Any]) -> bool:
    value = _first_deep_contract_value(line, "db_verified")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _latest_line_by_id(
    lines: Sequence[Mapping[str, Any]],
    line_id: str,
) -> Mapping[str, Any]:
    for line in reversed(list(lines)):
        if (
            isinstance(line, Mapping)
            and str(line.get("line_id") or "").strip() == line_id
        ):
            return line
    return {}


def _synthetic_graph_skip_line(
    *,
    stage_id: str,
    line_id: str,
    actor_role: str,
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "line_id": line_id,
        "actor_role": actor_role,
        "evidence_kind": "graph_trace",
        "status": "compat_skipped",
        "payload": {
            "schema_version": "contract_runtime.graph_context_compat_skip.v1",
            "source": "contract_runtime_feature_compatibility",
            "db_verified": False,
            "runtime_graph_gate_disabled": True,
        },
    }


def _compat_completion_lines_for_record(
    record: Mapping[str, Any],
    definition: Mapping[str, Any],
    line_items: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    features = _contract_runtime_features_for_record(record, definition)
    additions: list[Mapping[str, Any]] = []
    contract_id = _record_contract_id(record)
    if (
        contract_id == "direct_fix"
        and features.get("direct_fix_graph_query_gate") is False
    ):
        for stage_id, line_id, actor_role in (
            ("observer_graph_scope", "direct_fix_observer_graph_scope", "observer"),
            ("worker_graph_context", "direct_fix_worker_graph_context", "mf_sub"),
            ("qa_graph_context", "direct_fix_qa_graph_context", "qa"),
        ):
            if not _line_id_present(line_items, line_id):
                additions.append(
                    _synthetic_graph_skip_line(
                        stage_id=stage_id,
                        line_id=line_id,
                        actor_role=actor_role,
                    )
                )
    if contract_id in {"mf_parallel", "mf_parallel.v2"}:
        worker_graph = _latest_line_by_id(line_items, "worker_graph_context")
        if (
            worker_graph
            and not _line_id_present(line_items, "qa_graph_context")
            and not _contract_graph_line_db_verified(worker_graph)
        ):
            additions.append(
                _synthetic_graph_skip_line(
                    stage_id="qa_graph_context",
                    line_id="qa_graph_context",
                    actor_role="qa",
                )
            )
    if not additions:
        return list(line_items)
    return [*list(line_items), *additions]


def _active_failed_qa_line_index(lines: Sequence[Mapping[str, Any]]) -> int:
    failed_index = _last_failed_qa_line_index(lines)
    if failed_index < 0:
        return -1
    for index, line in enumerate(lines):
        if index <= failed_index or not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() != "qa_independent_verification":
            continue
        if not _line_shape_allows_contract_completion(line):
            continue
        if _line_status_allows_contract_completion(line):
            return -1
    return failed_index


def _attach_failed_qa_rework_guidance(
    guide: dict[str, Any],
    *,
    line_items: Sequence[Mapping[str, Any]],
) -> None:
    failed_index = _active_failed_qa_line_index(line_items)
    if failed_index < 0:
        return
    next_action = guide.get("next_legal_action")
    if not isinstance(next_action, dict) or not next_action:
        return
    failed_line = line_items[failed_index]
    blocker = {
        "schema_version": "contract_runtime.failed_qa_rework_guidance.v1",
        "status": "blocked_by_failed_independent_qa",
        "semantic_next_action": "revise_after_failed_independent_qa",
        "failed_qa_completed_line_index": failed_index,
        "failed_qa_line_id": str(failed_line.get("line_id") or ""),
        "failed_qa_stage_id": str(failed_line.get("stage_id") or ""),
        "failed_qa_status": str(failed_line.get("status") or ""),
        "next_required_line_id": str(next_action.get("line_id") or ""),
        "next_required_owner_role": str(next_action.get("owner_role") or ""),
        "reason": (
            "Independent QA recorded a failing verdict; merge/materialize stay "
            "blocked until a worker revision and a later passing independent QA line."
        ),
    }
    next_action.setdefault("semantic_next_action", blocker["semantic_next_action"])
    next_action.setdefault("blocked_by_failed_qa", True)
    next_action.setdefault("failed_qa_blocker", blocker)
    guide.setdefault("failed_qa_rework", blocker)


def _last_failed_qa_line_index(lines: Sequence[Mapping[str, Any]]) -> int:
    failed_index = -1
    for index, line in enumerate(lines):
        if not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() != "qa_independent_verification":
            continue
        if not _line_status_allows_contract_completion(line):
            failed_index = index
    return failed_index


_FAILED_QA_RETRY_RESET_LINE_IDS = frozenset(
    {
        "direct_fix_worker_graph_context",
        "direct_fix_qa_graph_context",
        "worker_read_runtime_guide",
        "worker_startup",
        "worker_graph_context",
        "worker_implementation",
        "worker_commit",
        "worker_finish_time_attestation",
        "worker_finish_gate",
        "worker_review_ready_handoff",
        "qa_independent_verification",
        "observer_merge",
        "observer_reconcile",
        "observer_close_ready",
    }
)

_FAILED_QA_RETRY_SETUP_LINE_IDS = frozenset(
    {
        "worker_read_runtime_guide",
        "worker_startup",
        "worker_graph_context",
    }
)

_FAILED_QA_RETRY_PROOF_LINE_IDS = frozenset(
    {
        "worker_implementation",
        "worker_commit",
        "worker_finish_time_attestation",
        "worker_finish_gate",
        "worker_review_ready_handoff",
        "qa_independent_verification",
        "observer_merge",
        "observer_reconcile",
        "observer_close_ready",
    }
)


def _post_failed_qa_retry_context_keys(
    lines: Sequence[Mapping[str, Any]],
    *,
    failed_qa_index: int,
) -> set[tuple[str, str]]:
    if failed_qa_index < 0:
        return set()
    contexts: set[tuple[str, str]] = set()
    for line in lines[failed_qa_index + 1 :]:
        if not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() not in _FAILED_QA_RETRY_PROOF_LINE_IDS:
            continue
        if not _line_status_allows_contract_completion(line):
            continue
        contexts.update(_line_retry_context_keys(line))
    return contexts


def _failed_qa_rejoin_context_keys_from_projection(
    projection: Mapping[str, Any] | None,
) -> set[tuple[str, str]]:
    if not isinstance(projection, Mapping):
        return set()
    contexts: set[tuple[str, str]] = set()
    for item in projection.get("failed_qa_revision_rejoin_contexts") or []:
        if not isinstance(item, Mapping):
            continue
        runtime_context_id = str(item.get("runtime_context_id") or "").strip()
        task_id = str(item.get("task_id") or "").strip()
        if runtime_context_id:
            contexts.add(("runtime_context_id", runtime_context_id))
            contexts.add(("line_instance_id", f"runtime_context:{runtime_context_id}"))
        if task_id:
            contexts.add(("task_id", task_id))
            contexts.add(("line_instance_id", f"task:{task_id}"))
    return contexts


def _failed_qa_rejoin_markers_from_projection(
    projection: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(projection, Mapping):
        return []
    markers: list[dict[str, Any]] = []
    for item in projection.get("failed_qa_revision_rejoin_contexts") or []:
        if not isinstance(item, Mapping):
            continue
        source_ref = str(
            item.get("revision_event_ref") or item.get("source_ref") or ""
        ).strip()
        if not source_ref:
            continue
        context_keys: set[tuple[str, str]] = set()
        runtime_context_id = str(item.get("runtime_context_id") or "").strip()
        task_id = str(item.get("task_id") or "").strip()
        if runtime_context_id:
            context_keys.add(("runtime_context_id", runtime_context_id))
            context_keys.add(
                ("line_instance_id", f"runtime_context:{runtime_context_id}")
            )
        if task_id:
            context_keys.add(("task_id", task_id))
            context_keys.add(("line_instance_id", f"task:{task_id}"))
        if context_keys:
            markers.append(
                {
                    "source_ref": source_ref,
                    "context_keys": context_keys,
                }
            )
    return markers


def _line_matches_failed_qa_rejoin_marker(
    line: Mapping[str, Any],
    markers: Sequence[Mapping[str, Any]],
) -> bool:
    marker = _first_deep_contract_value(
        line,
        "failed_qa_revision_rejoin_marker",
    )
    if not isinstance(marker, Mapping):
        return False
    source_ref = str(
        marker.get("revision_event_ref") or marker.get("source_ref") or ""
    ).strip()
    if not source_ref:
        return False
    line_contexts = _line_retry_context_keys(line)
    for expected in markers:
        expected_contexts = expected.get("context_keys")
        if not isinstance(expected_contexts, set):
            continue
        if (
            source_ref == str(expected.get("source_ref") or "").strip()
            and line_contexts.intersection(expected_contexts)
        ):
            return True
    return False


def _failed_qa_rejoin_boundaries(
    lines: Sequence[Mapping[str, Any]],
    markers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for marker in markers:
        context_keys = marker.get("context_keys")
        if not isinstance(context_keys, set):
            continue
        boundary_index = -1
        for index, line in enumerate(lines):
            if isinstance(line, Mapping) and _line_matches_failed_qa_rejoin_marker(
                line,
                [marker],
            ):
                boundary_index = index
                break
        boundaries.append(
            {
                "source_ref": str(marker.get("source_ref") or "").strip(),
                "context_keys": context_keys,
                "line_index": boundary_index,
            }
        )
    return boundaries


def _line_survives_failed_qa_rejoin_boundary(
    line: Mapping[str, Any],
    *,
    index: int,
    boundaries: Sequence[Mapping[str, Any]],
) -> bool:
    line_contexts = _line_retry_context_keys(line)
    applicable = [
        boundary
        for boundary in boundaries
        if isinstance(boundary.get("context_keys"), set)
        and line_contexts.intersection(boundary["context_keys"])
    ]
    if not applicable:
        return True
    return all(
        isinstance(boundary.get("line_index"), int)
        and boundary["line_index"] >= 0
        and index >= boundary["line_index"]
        for boundary in applicable
    )


def _line_retry_context_keys(line: Mapping[str, Any]) -> set[tuple[str, str]]:
    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    keys: set[tuple[str, str]] = set()
    line_instance_id = _line_instance_id_from_mapping(line)
    if line_instance_id:
        keys.add(("line_instance_id", line_instance_id))
    for field in ("runtime_context_id", "task_id", "parent_task_id"):
        value = _first_mapping_text(line, field) or _first_mapping_text(payload, field)
        if not value:
            continue
        keys.add((field, value))
        if field == "runtime_context_id":
            keys.add(("line_instance_id", f"runtime_context:{value}"))
        elif field == "task_id":
            keys.add(("line_instance_id", f"task:{value}"))
    lane_id = (
        _first_mapping_text(line, "lane_id", "worker_slot_id", "worker_id")
        or _first_mapping_text(payload, "lane_id", "worker_slot_id", "worker_id")
    )
    if lane_id:
        keys.add(("lane_id", lane_id))
        keys.add(("line_instance_id", f"lane:{lane_id}"))
    return keys


def _line_survives_failed_qa_retry_reset(
    line: Mapping[str, Any],
    *,
    post_failed_retry_contexts: set[tuple[str, str]] | None = None,
) -> bool:
    actor_role = str(line.get("actor_role") or "").strip().lower().replace("-", "_")
    line_id = str(line.get("line_id") or "").strip()
    if (
        line_id in _FAILED_QA_RETRY_SETUP_LINE_IDS
        and post_failed_retry_contexts
        and _line_retry_context_keys(line).intersection(post_failed_retry_contexts)
    ):
        return True
    if actor_role in {"mf_sub", "qa"}:
        return False
    if line_id in _FAILED_QA_RETRY_RESET_LINE_IDS:
        return False
    return True


_LINE_PAYLOAD_SCHEMA_BLOCKLIST = {
    "worker_implementation": frozenset(
        {
            "worker_runtime_guide_read_after_failed_qa_revision.v1",
            "contract_context_read_receipt.v1",
            "mf_subagent_read_receipt.v1",
            "mf_subagent_startup.v1",
            "worker_graph_context.v1",
        }
    ),
}


_WORKER_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _worker_commit_mapping_candidates(value: Any, *, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 6:
        return []
    if isinstance(value, Mapping):
        candidates: list[Mapping[str, Any]] = [value]
        for child in value.values():
            candidates.extend(_worker_commit_mapping_candidates(child, depth=depth + 1))
        return candidates
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        candidates = []
        for child in value:
            candidates.extend(_worker_commit_mapping_candidates(child, depth=depth + 1))
        return candidates
    return []


def _worker_commit_text(value: Any, *keys: str) -> str:
    for candidate in _worker_commit_mapping_candidates(value):
        for key in keys:
            text = str(candidate.get(key) or "").strip()
            if text:
                return text
    return ""


def _worker_commit_strings(value: Any, *keys: str) -> list[str]:
    values: list[str] = []
    for candidate in _worker_commit_mapping_candidates(value):
        for key in keys:
            raw = candidate.get(key)
            if isinstance(raw, str):
                items = [raw]
            elif isinstance(raw, Sequence) and not isinstance(
                raw,
                (str, bytes, bytearray),
            ):
                items = list(raw)
            else:
                continue
            for item in items:
                text = str(item or "").strip()
                if text and text not in values:
                    values.append(text)
    return values


def _worker_commit_flag(value: Any, *keys: str) -> bool:
    for candidate in _worker_commit_mapping_candidates(value):
        for key in keys:
            if key in candidate and _truthy_contract_flag(candidate.get(key)):
                return True
    return False


def _worker_commit_completed_implementation(
    record: Mapping[str, Any],
    *,
    runtime_context_id: str,
    task_id: str,
) -> Mapping[str, Any] | None:
    for line in reversed(list(record.get("completed_lines") or [])):
        if not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() != "worker_implementation":
            continue
        line_runtime_context_id = _worker_commit_text(line, "runtime_context_id")
        line_task_id = _worker_commit_text(line, "task_id")
        if runtime_context_id and line_runtime_context_id != runtime_context_id:
            continue
        if task_id and line_task_id != task_id:
            continue
        return line
    return None


def _mf_parallel_worker_commit_errors(
    record: Mapping[str, Any],
    write: Mapping[str, Any],
    *,
    actor_role: str,
) -> tuple[str, ...]:
    if str(write.get("line_id") or "").strip() != "worker_commit":
        return ()
    if _record_contract_id(record) not in {"mf_parallel", "mf_parallel.v2"}:
        return ()

    errors: list[str] = []
    if actor_role != "mf_sub":
        errors.append("worker_commit requires actor_role=mf_sub")
    evidence_owner_role = _worker_commit_text(write, "evidence_owner_role", "worker_role")
    if evidence_owner_role != "mf_sub":
        errors.append("worker_commit requires mf_sub evidence ownership")
    if _worker_commit_flag(write, "observer_impersonation", "filed_on_behalf", "on_behalf"):
        errors.append("worker_commit rejects observer impersonation or on-behalf evidence")

    required_text_fields = {
        "runtime_context_id": ("runtime_context_id",),
        "task_id": ("task_id",),
        "parent_task_id": ("parent_task_id",),
        "worker_id": ("worker_id",),
        "worker_slot_id": ("worker_slot_id", "lane_id"),
        "worker_session_id": ("worker_session_id",),
        "actor_session_principal": ("actor_session_principal",),
        "implementation_event_ref": ("implementation_event_ref",),
        "target_project_root": ("target_project_root",),
        "session_token_ref": ("session_token_ref", "evidence_owner_session_ref"),
        "fence_token_hash": ("fence_token_hash",),
    }
    resolved: dict[str, str] = {}
    for field, aliases in required_text_fields.items():
        resolved[field] = _worker_commit_text(write, *aliases)
        if not resolved[field]:
            errors.append(f"worker_commit requires {field}")

    commit_sha = str(write.get("commit_sha") or "").strip() or _worker_commit_text(
        write,
        "commit_sha",
        "worker_commit_sha",
    )
    if not _WORKER_COMMIT_SHA_RE.fullmatch(commit_sha):
        errors.append("worker_commit requires a full immutable commit_sha")
    for field in ("head_commit", "immutable_head_commit", "validated_head_commit"):
        value = _worker_commit_text(write, field)
        if value != commit_sha:
            errors.append(f"worker_commit {field} must equal commit_sha")

    worker_session_id = resolved.get("worker_session_id", "")
    if worker_session_id and resolved.get("actor_session_principal") != worker_session_id:
        errors.append("worker_commit actor_session_principal must match worker_session_id")
    filer_principal = _worker_commit_text(write, "filer_principal", "submitter_principal")
    if filer_principal != worker_session_id:
        errors.append("worker_commit filer_principal must match worker_session_id")

    clean_worktree = _worker_commit_flag(write, "clean_worktree", "worktree_clean")
    if not clean_worktree:
        errors.append("worker_commit requires clean_worktree=true")
    if _worker_commit_strings(write, "dirty_files", "worktree_status"):
        errors.append("worker_commit rejects dirty worktree evidence")

    changed_files = set(_worker_commit_strings(write, "changed_files"))
    commit_diff_files = set(_worker_commit_strings(write, "commit_diff_files"))
    owned_files = set(_worker_commit_strings(write, "owned_files"))
    if not changed_files:
        errors.append("worker_commit requires non-empty changed_files")
    if changed_files != commit_diff_files:
        errors.append("worker_commit changed_files must exactly match commit_diff_files")
    if not owned_files:
        errors.append("worker_commit requires owned_files")
    out_of_fence = sorted(changed_files - owned_files)
    if out_of_fence:
        errors.append(f"worker_commit contains out-of-fence files: {out_of_fence!r}")

    graph_trace_ids = set(
        _worker_commit_strings(
            write,
            "graph_trace_ids",
            "graph_query_trace_ids",
            "verified_trace_ids",
        )
    )
    if not graph_trace_ids:
        errors.append("worker_commit requires DB graph trace ids")
    if not _worker_commit_flag(write, "db_verified"):
        errors.append("worker_commit requires db_verified graph trace evidence")

    implementation = _worker_commit_completed_implementation(
        record,
        runtime_context_id=resolved.get("runtime_context_id", ""),
        task_id=resolved.get("task_id", ""),
    )
    if implementation is None:
        errors.append("worker_commit requires matching worker_implementation lineage")
    else:
        implementation_changed_files = set(
            _worker_commit_strings(implementation, "changed_files")
        )
        if implementation_changed_files != changed_files:
            errors.append("worker_commit diff does not match worker_implementation changed_files")
        implementation_graph_trace_ids = set(
            _worker_commit_strings(
                implementation,
                "graph_trace_ids",
                "graph_query_trace_ids",
                "verified_trace_ids",
            )
        )
        if implementation_graph_trace_ids != graph_trace_ids:
            errors.append("worker_commit graph traces do not match worker_implementation lineage")
        for field in ("worker_id", "worker_slot_id"):
            implementation_value = _worker_commit_text(implementation, field)
            if implementation_value and implementation_value != resolved.get(field, ""):
                errors.append(f"worker_commit {field} does not match worker_implementation")
    return tuple(dict.fromkeys(errors))


def _gate_decision_with_additional_errors(
    decision: Any,
    errors: Sequence[str],
) -> Any:
    if not errors:
        return decision
    return make_gate_decision(
        action=str(getattr(decision, "action", "") or "submit_line"),
        gate_id=str(getattr(decision, "gate_id", "") or "worker_commit_proof"),
        errors=(*tuple(getattr(decision, "errors", ()) or ()), *tuple(errors)),
        warnings=tuple(getattr(decision, "warnings", ()) or ()),
        next_move=getattr(decision, "next_move", {}) or {},
        gate_type=str(getattr(decision, "gate_type", "") or "line_proof"),
        stage_id=str(getattr(decision, "stage_id", "") or ""),
        line_id=str(getattr(decision, "line_id", "") or ""),
        required_role=str(getattr(decision, "required_role", "") or ""),
        actor_role=str(getattr(decision, "actor_role", "") or ""),
        missing_lines=tuple(getattr(decision, "missing_lines", ()) or ()),
        missing_proof_fields=tuple(getattr(decision, "missing_proof_fields", ()) or ()),
        hash_status=getattr(decision, "hash_status", {}) or {},
        graph_status=getattr(decision, "graph_status", {}) or {},
        dirty_scope_status=getattr(decision, "dirty_scope_status", {}) or {},
        imported_legacy_checks=tuple(getattr(decision, "imported_legacy_checks", ()) or ()),
        projection_actions=tuple(getattr(decision, "projection_actions", ()) or ()),
        policy_hash=str(getattr(decision, "policy_hash", "") or ""),
        contract_definition_hash=str(
            getattr(decision, "contract_definition_hash", "") or ""
        ),
        execution_state_revision=int(
            getattr(decision, "execution_state_revision", 0) or 0
        ),
        runtime_guide_hash=str(getattr(decision, "runtime_guide_hash", "") or ""),
    )


def _line_shape_allows_contract_completion(line: Mapping[str, Any]) -> bool:
    line_id = str(line.get("line_id") or "").strip()
    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    schema_version = str(payload.get("schema_version") or "").strip()
    blocked_schemas = _LINE_PAYLOAD_SCHEMA_BLOCKLIST.get(line_id)
    return not (blocked_schemas and schema_version in blocked_schemas)


def _line_status_allows_contract_completion(line: Mapping[str, Any]) -> bool:
    if _mapping_own_fields_contain_contract_completion_blocker(line):
        return False
    for field in ("qa_evidence_provenance", "verification"):
        if _contains_contract_completion_blocker(line.get(field)):
            return False
    if str(line.get("line_id") or "").strip() == "qa_independent_verification":
        payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
        if _contains_contract_completion_blocker(payload):
            return False
        if _qa_independent_verification_summary_reports_failure(payload):
            return False
    return True


_QA_FAILURE_SUMMARY_FIELDS = frozenset(
    {
        "summary",
        "reason",
        "decision_summary",
        "qa_summary",
        "qa_result_summary",
        "verification_summary",
        "failure_summary",
        "block_reason",
    }
)
_QA_FAILURE_TEXT_MARKERS = (
    "independent qa failed",
    "qa failed",
    "qa rejected",
    "qa blocked",
    "verification failed",
    "verification rejected",
    "failed worker commit",
    "failed the worker commit",
)
_QA_PASSING_TEXT_MARKERS = (
    "independent qa passed",
    "qa passed",
    "verification passed",
    "passed qa",
)


def _qa_independent_verification_summary_reports_failure(value: Any) -> bool:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key or "").strip().lower()
            if key in _QA_FAILURE_SUMMARY_FIELDS and _qa_failure_text_signal(item):
                return True
            if _qa_independent_verification_summary_reports_failure(item):
                return True
        return False
    if isinstance(value, list):
        return any(_qa_independent_verification_summary_reports_failure(item) for item in value)
    return False


def _qa_failure_text_signal(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = " ".join(
        value.strip().lower().replace("-", " ").replace("_", " ").split()
    )
    if not text:
        return False
    if any(marker in text for marker in _QA_PASSING_TEXT_MARKERS):
        return False
    return any(marker in text for marker in _QA_FAILURE_TEXT_MARKERS)


def _mapping_own_fields_contain_contract_completion_blocker(
    value: Mapping[str, Any],
) -> bool:
    for raw_key, item in value.items():
        key = str(raw_key or "").strip().lower()
        if (
            key in _CONTRACT_COMPLETION_STATUS_FIELDS
            and str(item or "").strip().lower()
            in _CONTRACT_COMPLETION_BLOCKING_STATUSES
        ):
            return True
        if key in _CONTRACT_COMPLETION_FAILURE_COUNT_FIELDS and _truthy_failure_count(
            item
        ):
            return True
    return False


def _contains_contract_completion_blocker(value: Any) -> bool:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key or "").strip().lower()
            if (
                key in _CONTRACT_COMPLETION_STATUS_FIELDS
                and str(item or "").strip().lower()
                in _CONTRACT_COMPLETION_BLOCKING_STATUSES
            ):
                return True
            if key in _CONTRACT_COMPLETION_FAILURE_COUNT_FIELDS and _truthy_failure_count(
                item
            ):
                return True
            if _contains_contract_completion_blocker(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_contract_completion_blocker(item) for item in value)
    return False


def _truthy_failure_count(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value > 0
    try:
        return int(str(value or "").strip()) > 0
    except (TypeError, ValueError):
        return False


def _line_scope_matches_direct_fix_child(
    line: Mapping[str, Any],
    *,
    contract_execution_id: str,
    generation: int,
) -> bool:
    evidence_scope = _line_evidence_scope(line)
    execution_refs = set(
        _deep_text_values(
            evidence_scope,
            {
                "contract_execution_id",
                "direct_fix_contract_execution_id",
                "child_contract_execution_id",
                "successor_contract_execution_id",
            },
        )
    )
    if contract_execution_id not in execution_refs:
        return False
    generation_refs = set(
        _deep_text_values(
            evidence_scope,
            {
                "generation",
                "projection_generation",
                "execution_state_revision",
            },
        )
    )
    if not generation_refs or not _generation_refs_allow_current(
        generation_refs,
        generation=generation,
    ):
        return False
    source_refs = set(
        _deep_text_values(
            evidence_scope,
            {
                "source_ref",
                "source_refs",
                "artifact_ref",
                "evidence_ref",
                "evidence_refs",
                "repair_evidence_ref",
                "repair_evidence_refs",
                "source_evidence_ref",
                "source_evidence_refs",
            },
        )
    )
    source_refs.update(_top_level_artifact_ref_values(line))
    if not source_refs:
        return False
    return True


def _line_evidence_scope(line: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key in ("payload", "verification", "artifact_refs")
        if (value := line.get(key)) is not None
    }


def _generation_refs_allow_current(
    generation_refs: set[str],
    *,
    generation: int,
) -> bool:
    for ref in generation_refs:
        try:
            value = int(str(ref).strip())
        except ValueError:
            continue
        if 0 < value <= generation:
            return True
    return False


def _top_level_artifact_ref_values(line: Mapping[str, Any]) -> list[str]:
    artifact_refs = line.get("artifact_refs")
    if isinstance(artifact_refs, list) or isinstance(artifact_refs, str):
        return _flatten_text_values(artifact_refs)
    return []


def _flatten_text_values(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, Mapping):
        values: list[str] = []
        for child in value.values():
            values.extend(_flatten_text_values(child, depth=depth + 1))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_flatten_text_values(child, depth=depth + 1))
        return values
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        return [text] if text else []
    return []


def _deep_text_values(value: Any, keys: set[str], *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text in keys:
                values.extend(_flatten_text_values(child, depth=depth + 1))
            values.extend(_deep_text_values(child, keys, depth=depth + 1))
    elif isinstance(value, list):
        for child in value:
            values.extend(_deep_text_values(child, keys, depth=depth + 1))
    return values


def _direct_fix_qa_next_action(
    child: Mapping[str, Any],
    *,
    generation: int,
    repair_line: Mapping[str, Any],
    qa_graph_line: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    child_id = str(child.get("contract_execution_id") or "")
    action = {
        "schema_version": "backlog_contract_chain.next_action.v1",
        "id": "qa_independent_verification",
        "action": "record_direct_fix_independent_qa",
        "source": "backlog_contract_chain_current",
        "precedence": "direct_fix_qa_gate",
        "role": "qa",
        "work_type": "qa_verification",
        "contract_execution_id": child_id,
        "parent_contract_execution_id": str(
            child.get("parent_contract_execution_id") or ""
        ),
        "root_contract_execution_id": str(child.get("root_contract_execution_id") or ""),
        "contract_chain_id": str(child.get("contract_chain_id") or ""),
        "contract_id": _record_contract_id(child),
        "stage_id": "qa",
        "line_id": "qa_independent_verification",
        "evidence_kind": "independent_verification",
        "required": True,
        "required_binding": {
            "contract_execution_id": child_id,
            "generation": generation,
            "source_ref": f"contract_runtime:{child_id}:completed_lines:{repair_line.get('_completed_line_index', '')}",
        },
        "meta_contract_gate_decision_source": False,
    }
    qa_graph_index = _completed_line_index(qa_graph_line or {})
    if qa_graph_index >= 0:
        action["qa_graph_evidence_ref"] = (
            f"contract_runtime:{child_id}:completed_lines:{qa_graph_index}"
        )
    return action


def _direct_fix_qa_graph_next_action(
    child: Mapping[str, Any],
    *,
    generation: int,
    repair_line: Mapping[str, Any],
) -> dict[str, Any]:
    child_id = str(child.get("contract_execution_id") or "")
    return {
        "schema_version": "backlog_contract_chain.next_action.v1",
        "id": "direct_fix_qa_graph_context",
        "action": "record_direct_fix_qa_graph_context",
        "source": "backlog_contract_chain_current",
        "precedence": "direct_fix_qa_graph_gate",
        "role": "qa",
        "work_type": "qa_verification",
        "contract_execution_id": child_id,
        "parent_contract_execution_id": str(
            child.get("parent_contract_execution_id") or ""
        ),
        "root_contract_execution_id": str(child.get("root_contract_execution_id") or ""),
        "contract_chain_id": str(child.get("contract_chain_id") or ""),
        "contract_id": _record_contract_id(child),
        "stage_id": "qa_graph_context",
        "line_id": "direct_fix_qa_graph_context",
        "evidence_kind": "graph_trace",
        "required": True,
        "required_binding": {
            "contract_execution_id": child_id,
            "generation": generation,
            "source_ref": f"contract_runtime:{child_id}:completed_lines:{repair_line.get('_completed_line_index', '')}",
        },
        "graph_query_packet": {
            "query_source": "qa",
            "query_purpose": "independent_verification",
            "runtime_context_binding_required": False,
            "source_ref": f"contract_runtime:{child_id}:completed_lines:{repair_line.get('_completed_line_index', '')}",
        },
        "meta_contract_gate_decision_source": False,
    }


def _direct_fix_return_next_action(
    child: Mapping[str, Any],
    *,
    parent_id: str,
    generation: int,
    qa_line: Mapping[str, Any],
) -> dict[str, Any]:
    child_id = str(child.get("contract_execution_id") or "")
    return {
        "schema_version": "backlog_contract_chain.next_action.v1",
        "id": "return_to_parent_after_direct_fix_qa",
        "action": "record_direct_fix_return_to_parent",
        "source": "backlog_contract_chain_current",
        "precedence": "direct_fix_independent_qa_passed",
        "role": "observer",
        "contract_execution_id": child_id,
        "parent_contract_execution_id": parent_id,
        "root_contract_execution_id": str(child.get("root_contract_execution_id") or ""),
        "contract_chain_id": str(child.get("contract_chain_id") or ""),
        "contract_id": _record_contract_id(child),
        "stage_id": "return_to_parent",
        "line_id": "direct_fix_return_to_parent",
        "evidence_kind": "direct_fix_return_to_parent",
        "required": True,
        "qa_evidence_ref": qa_line.get("_source_ref", ""),
        "generation": generation,
        "meta_contract_gate_decision_source": False,
    }


def _parent_resume_next_action(
    child: Mapping[str, Any],
    *,
    parent_id: str,
    generation: int,
    return_line: Mapping[str, Any],
) -> dict[str, Any]:
    child_id = str(child.get("contract_execution_id") or "")
    return {
        "schema_version": "backlog_contract_chain.next_action.v1",
        "id": "resume_parent_after_successor_return",
        "action": "resume_parent_after_successor_return",
        "source": "backlog_contract_chain_current",
        "precedence": "direct_fix_return_recorded",
        "contract_execution_id": parent_id,
        "successor_contract_execution_id": child_id,
        "parent_contract_execution_id": parent_id,
        "root_contract_execution_id": str(child.get("root_contract_execution_id") or ""),
        "contract_chain_id": str(child.get("contract_chain_id") or ""),
        "stage_id": "successor_return",
        "line_id": "resume_parent_after_successor_return",
        "evidence_kind": "successor_return_acknowledgement",
        "owner_role": "observer",
        "allowed_writer_roles": ["observer"],
        "parent_close_gate_recheck_required": True,
        "child_must_not_write_parent_close_evidence": True,
        "return_line_ref": f"contract_runtime:{child_id}:completed_lines:{return_line.get('_completed_line_index', '')}",
        "generation": generation,
        "meta_contract_gate_decision_source": False,
    }


def _projection_source_refs(records: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for record in records:
        execution_id = str(record.get("contract_execution_id") or "")
        if not execution_id:
            continue
        refs.append(
            f"contract_runtime:{execution_id}:revision:{int(record.get('execution_state_revision') or 0)}"
        )
    return refs


def _current_projection_from_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    data = dict(row) if isinstance(row, sqlite3.Row) else {}
    if not data:
        return {}
    degraded_flags = _json_field(data.get("degraded_flags_json"), {})
    next_legal_action = _json_field(data.get("next_legal_action_json"), {})
    active_chain = _json_field(data.get("active_chain_json"), {})
    source_refs = _json_field(data.get("source_refs_json"), [])
    return {
        "schema_version": "backlog_contract_chain_current.v1",
        "project_id": str(data.get("project_id") or ""),
        "backlog_id": str(data.get("backlog_id") or ""),
        "contract_chain_id": str(data.get("contract_chain_id") or ""),
        "active_chain": active_chain if isinstance(active_chain, dict) else {},
        "root_contract_execution_id": str(
            data.get("root_contract_execution_id") or ""
        ),
        "current_contract_execution_id": str(
            data.get("current_contract_execution_id") or ""
        ),
        "current_contract_id": str(data.get("current_contract_id") or ""),
        "parent_to_resume_contract_execution_id": str(
            data.get("parent_to_resume_contract_execution_id") or ""
        ),
        "active_child_contract_execution_id": str(
            data.get("active_child_contract_execution_id") or ""
        ),
        "readiness_state": str(data.get("readiness_state") or ""),
        "generation": int(data.get("generation") or 0),
        "next_legal_action": (
            next_legal_action if isinstance(next_legal_action, dict) else {}
        ),
        "projection_watermark": int(data.get("projection_watermark") or 0),
        "projection_hash": str(data.get("projection_hash") or ""),
        "degraded_flags": degraded_flags if isinstance(degraded_flags, dict) else {},
        "degraded": bool(degraded_flags),
        "source_refs": source_refs if isinstance(source_refs, list) else [],
        "updated_at": str(data.get("updated_at") or ""),
        "projection_source": "backlog_contract_chain_current",
        "source_of_proof": "contract_runtime_executions.completed_lines",
    }


def _json_field(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return fallback
    return value


def _judgment_hints_disabled() -> bool:
    value = str(os.environ.get(_JUDGMENT_HINTS_DISABLED_ENV) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _runtime_judgment_hints_task_id(
    *,
    backlog_id: str,
    contract_execution_id: str,
    backlog_lineage: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> str:
    for source in (metadata, backlog_lineage):
        if not isinstance(source, Mapping):
            continue
        for key in ("task_id", "parent_task_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return str(backlog_id or contract_execution_id or "").strip()


def _default_judgment_hints_fetcher(
    *,
    project_id: str,
    task_id: str,
    timeout: float = _JUDGMENT_HINT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    port = str(os.environ.get(_JUDGMENT_HINT_PORT_ENV) or _JUDGMENT_HINT_DEFAULT_PORT)
    port = port.strip() or _JUDGMENT_HINT_DEFAULT_PORT
    query = urllib.parse.urlencode({"project_id": project_id, "task_id": task_id})
    url = f"http://127.0.0.1:{port}/hints?{query}"
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", response.getcode()) or 0)
        body = response.read().decode("utf-8")
    return status, body


def _decode_judgment_hints_fetch_result(result: Any) -> Any:
    status = 200
    payload = result
    if isinstance(result, tuple) and len(result) == 2:
        status = int(result[0] or 0)
        payload = result[1]
    if status != 200:
        raise ValueError(f"judgment_hints_non_200:{status}")
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        return json.loads(payload)
    return payload


def _normalize_judgment_hints_payload(payload: Any) -> list[Any] | None:
    raw_hints = payload
    if isinstance(payload, Mapping):
        raw_hints = payload.get("judgment_hints", payload.get("hints"))
    if not isinstance(raw_hints, list) or not raw_hints:
        return None
    json.dumps(raw_hints, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    hints = deepcopy(raw_hints)
    return hints if hints else None


def _fetch_judgment_hints(
    *,
    project_id: str,
    task_id: str,
    fetcher: Any = None,
) -> list[Any] | None:
    if _judgment_hints_disabled():
        log.info("judgment_hints_gap: disabled by %s", _JUDGMENT_HINTS_DISABLED_ENV)
        return None
    project_id = str(project_id or "").strip()
    task_id = str(task_id or "").strip()
    if not project_id or not task_id:
        log.info("judgment_hints_gap: missing project_id or task_id")
        return None
    fetch = fetcher or _default_judgment_hints_fetcher
    try:
        result = fetch(
            project_id=project_id,
            task_id=task_id,
            timeout=_JUDGMENT_HINT_TIMEOUT_SECONDS,
        )
        payload = _decode_judgment_hints_fetch_result(result)
        hints = _normalize_judgment_hints_payload(payload)
    except (
        OSError,
        TimeoutError,
        TypeError,
        ValueError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as exc:
        log.info("judgment_hints_gap: fetch failed open: %s", exc)
        return None
    if not hints:
        log.info("judgment_hints_gap: empty or missing hints")
        return None
    return hints


def _record_judgment_hints(record: Mapping[str, Any]) -> list[Any] | None:
    hints = record.get("judgment_hints")
    if not isinstance(hints, list) or not hints:
        return None
    return deepcopy(hints)


def _gate_decision_payload(decision: Any) -> dict[str, Any]:
    payload = decision.to_dict()
    errors = [str(item) for item in payload.get("errors") or []]
    has_detailed_hash_mismatch = any(
        error.startswith("runtime_guide_hash mismatch:") for error in errors
    )
    if has_detailed_hash_mismatch and "runtime_guide_hash mismatch" not in errors:
        payload["errors"] = ["runtime_guide_hash mismatch", *errors]
        payload["decision_hash"] = stable_sha256(
            {key: value for key, value in payload.items() if key != "decision_hash"}
        )
    return payload


_POST_PROJECTION_LINE_IDS = {
    "qa_independent_verification",
    "observer_merge",
    "observer_reconcile",
    "observer_close_ready",
}


def _completed_lines_projection_submit_guidance(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    projected_lines = projection.get("projected_completed_lines")
    if not isinstance(projected_lines, list):
        projected_lines = []
    projected_line_ids = [
        str(line.get("line_id") or "").strip()
        for line in projected_lines
        if isinstance(line, Mapping) and str(line.get("line_id") or "").strip()
    ]
    return {
        "schema_version": "contract_runtime.post_projection_submit_line_guidance.v1",
        "projection_present": True,
        "source": str(projection.get("source") or "").strip(),
        "projected_completed_lines_count": len(projected_lines),
        "projected_line_ids": projected_line_ids,
        "post_worker_line_ids": [
            line_id
            for line_id in projected_line_ids
            if line_id in _POST_PROJECTION_LINE_IDS
        ],
        "re_read_contract_runtime_current_required": True,
        "skip_duplicate_submit_line_when_projected_or_complete": True,
        "skip_duplicate_submit_line_when_target_line_projected_or_complete": True,
        "duplicate_submit_line_required": False,
        "raw_session_token_required": False,
        "raw_route_token_required": False,
        "raw_session_token_persisted": False,
        "raw_route_token_persisted": False,
        "message": (
            "After merge/reconcile auto-projection, re-read ContractRuntime "
            "current. If the target line is projected or complete, do not "
            "submit a duplicate line; only use no_mutation_expected precheck "
            "as a probe."
        ),
    }


def _attach_completed_lines_projection_guidance(
    guide: dict[str, Any],
    projection: Mapping[str, Any],
) -> None:
    guide["completed_lines_projection"] = dict(projection)
    guide["post_projection_submit_line_guidance"] = (
        _completed_lines_projection_submit_guidance(projection)
    )
    guide["runtime_guide_hash"] = stable_sha256(
        {
            key: value
            for key, value in guide.items()
            if key != "runtime_guide_hash"
        }
    )


class ContractRuntime:
    """Runtime facade that compiles guides and validates writes from state."""

    def __init__(
        self,
        registry: ContractDefinitionRegistry,
        *,
        instruction_root: str | Path | None = None,
        store: InMemoryContractExecutionStore | SQLiteContractExecutionStore | None = None,
        judgment_hints_fetcher: Any = None,
    ) -> None:
        self.registry = registry
        self.instruction_root = Path(instruction_root) if instruction_root is not None else registry.root
        self.store = store or InMemoryContractExecutionStore()
        self.gate_kernel = ContractGateKernel(registry)
        self.judgment_hints_fetcher = judgment_hints_fetcher

    def start_execution(
        self,
        contract_id: str,
        *,
        project_id: str,
        backlog_id: str,
        actor_role: str,
        contract_execution_id: str | None = None,
        version: str | None = None,
        revision: str | None = None,
        route_token_ref: str = "",
        parent_contract_execution_id: str = "",
        root_contract_execution_id: str = "",
        contract_chain_id: str = "",
        role_binding: Mapping[str, Any] | None = None,
        backlog_lineage: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if is_legacy_primary_contract_route(contract_id):
            raise ContractRuntimeError(
                "legacy_contract_route_blocked: legacy/meta/timeline routes "
                "cannot start primary ContractRuntime executions; start with "
                "onboard_contract and keep legacy routes audit-only"
            )
        definition = self.registry.get(
            contract_id,
            version=version,
            revision=revision,
            include_deprecated=True,
        )
        if not is_new_execution_allowed(definition):
            raise ContractRuntimeError(
                f"contract {definition['contract_id']}@{definition['version']}#{definition['revision']} "
                f"is {definition['status']} and cannot start new executions"
            )
        parent_contract = self._parent_contract_identity(parent_contract_execution_id)
        start_precheck = self.gate_kernel.precheck(
            definition,
            action="start_execution",
            actor_role=actor_role,
            subject={
                "project_id": project_id,
                "backlog_id": backlog_id,
                "contract_execution_id": contract_execution_id or "",
                "parent_contract_execution_id": parent_contract_execution_id,
                "root_contract_execution_id": root_contract_execution_id,
                "contract_chain_id": contract_chain_id,
                "route_token_ref": route_token_ref,
                "parent_contract": parent_contract,
            },
        )
        if start_precheck.decision == "block" and _enforce_start_precheck(definition):
            raise ContractRuntimeError("; ".join(start_precheck.errors))
        instruction_bundle = resolve_instruction_bundle(
            definition,
            root=self.instruction_root,
            include_content=True,
        )
        execution_id = contract_execution_id or f"cex-{uuid4().hex}"
        root_execution_id = root_contract_execution_id or execution_id
        chain_id = contract_chain_id or f"cchain-{uuid4().hex}"
        judgment_hints = _fetch_judgment_hints(
            project_id=project_id,
            task_id=_runtime_judgment_hints_task_id(
                backlog_id=backlog_id,
                contract_execution_id=execution_id,
                backlog_lineage=backlog_lineage,
                metadata=metadata,
            ),
            fetcher=self.judgment_hints_fetcher,
        )
        state = build_execution_state(
            definition,
            project_id=project_id,
            backlog_id=backlog_id,
            contract_execution_id=execution_id,
            actor_role=actor_role,
            route_token_ref=route_token_ref,
            instruction_bundle_hash=instruction_bundle["instruction_bundle_hash"],
        )
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
            judgment_hints=judgment_hints,
        )
        _attach_completed_line_evidence(guide, [])
        _attach_precheck_decision(guide, start_precheck.to_dict())
        record = {
            "schema_version": "contract_runtime_execution_record.v1",
            "project_id": project_id,
            "backlog_id": backlog_id,
            "contract_execution_id": execution_id,
            "parent_contract_execution_id": parent_contract_execution_id,
            "root_contract_execution_id": root_execution_id,
            "contract_chain_id": chain_id,
            "contract_id": definition["contract_id"],
            "version": definition["version"],
            "revision": definition["revision"],
            "definition_hash": definition["definition_hash"],
            "definition_source_sha256": str(definition.get("source_sha256") or ""),
            "definition_raw_source_sha256": str(definition.get("source_sha256") or ""),
            "definition_governance_hints_sha256": str(
                definition.get("governance_hints_sha256") or ""
            ),
            "instruction_bundle_hash": instruction_bundle["instruction_bundle_hash"],
            "route_token_ref": route_token_ref,
            "completed_lines": [],
            "execution_state_revision": state["execution_state_revision"],
            "execution_state": state,
            "runtime_guide": guide,
            "judgment_hints": judgment_hints,
            "precheck_decision": start_precheck.to_dict(),
            "role_binding": dict(role_binding or {}),
            "backlog_lineage": dict(backlog_lineage or {}),
            "metadata": dict(metadata or {}),
            "contract_runtime_features": _contract_runtime_features(definition),
        }
        return self.store.create(record)

    def _parent_contract_identity(
        self,
        parent_contract_execution_id: str,
    ) -> dict[str, Any]:
        if not parent_contract_execution_id:
            return {}
        parent = self.store.get(parent_contract_execution_id)
        return {
            "contract_execution_id": str(parent.get("contract_execution_id") or ""),
            "contract_id": str(parent.get("contract_id") or ""),
            "version": str(parent.get("version") or ""),
            "revision": str(parent.get("revision") or ""),
            "root_contract_execution_id": str(parent.get("root_contract_execution_id") or ""),
            "contract_chain_id": str(parent.get("contract_chain_id") or ""),
        }

    def current_guide(
        self,
        contract_execution_id: str,
        *,
        actor_role: str | None = None,
    ) -> dict[str, Any]:
        record = self.store.get(contract_execution_id)
        view = self._record_view(
            record,
            actor_role=actor_role,
            completed_lines=record.get("completed_lines") or [],
        )
        record["execution_state"] = view["execution_state"]
        record["runtime_guide"] = view["runtime_guide"]
        record["precheck_decision"] = view["precheck_decision"]
        self.store.update(contract_execution_id, record)
        return dict(view["runtime_guide"])

    def projected_record(
        self,
        contract_execution_id: str,
        *,
        actor_role: str | None = None,
        completed_lines: Sequence[Mapping[str, Any]],
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a non-mutating read model using source-backed projected lines."""

        record = self.store.get(contract_execution_id)
        return self._record_view(
            record,
            actor_role=actor_role,
            completed_lines=completed_lines,
            projection=projection,
        )

    def pinned_definition_has_line(
        self,
        contract_execution_id: str,
        line_id: str,
    ) -> bool:
        record = self.store.get(contract_execution_id)
        definition = self._load_pinned_definition(record)
        expected = str(line_id or "").strip()
        return bool(expected) and any(
            str(line.get("line_id") or "").strip() == expected
            for _stage, line in iter_stage_lines(definition)
        )

    def _record_view(
        self,
        record: Mapping[str, Any],
        *,
        actor_role: str | None = None,
        completed_lines: Sequence[Mapping[str, Any]] | None = None,
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        definition = self._load_pinned_definition(record)
        instruction_bundle = resolve_instruction_bundle(
            definition,
            root=self.instruction_root,
            include_content=True,
        )
        _assert_hash(
            "instruction_bundle_hash",
            record.get("instruction_bundle_hash"),
            instruction_bundle.get("instruction_bundle_hash"),
            record=record,
            definition=definition,
        )
        line_items = list(completed_lines or [])
        effective_actor_role = actor_role or str(
            record["execution_state"].get("actor_role") or ""
        )
        completion_input_lines = _compat_completion_lines_for_record(
            record,
            definition,
            line_items,
        )
        completion_satisfying_lines = _contract_completion_satisfying_lines(
            completion_input_lines,
            failed_qa_rejoin_contexts=(
                _failed_qa_rejoin_context_keys_from_projection(projection)
            ),
            failed_qa_rejoin_markers=(
                _failed_qa_rejoin_markers_from_projection(projection)
            ),
        )
        state = build_execution_state(
            definition,
            project_id=str(record["project_id"]),
            backlog_id=str(record["backlog_id"]),
            contract_execution_id=str(record["contract_execution_id"]),
            actor_role=effective_actor_role,
            completed_lines=completion_satisfying_lines,
            route_token_ref=str(record.get("route_token_ref") or ""),
            instruction_bundle_hash=str(record.get("instruction_bundle_hash") or ""),
            execution_state_revision=int(record.get("execution_state_revision") or 1),
        )
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
            judgment_hints=_record_judgment_hints(record),
        )
        _attach_failed_qa_rework_guidance(guide, line_items=line_items)
        _attach_completed_line_evidence(guide, line_items)
        sanitized_projection: dict[str, Any] = {}
        if projection:
            sanitized = _sanitize_line_evidence_value(projection)
            if isinstance(sanitized, Mapping):
                sanitized_projection = dict(sanitized)
                _attach_completed_lines_projection_guidance(
                    guide,
                    sanitized_projection,
                )
        _attach_writer_role_safe_submit_payload(
            guide,
            definition=definition,
            record=record,
            instruction_bundle=instruction_bundle,
            completed_lines=line_items,
            completion_satisfying_lines=completion_satisfying_lines,
            sanitized_projection=sanitized_projection,
            reader_role=effective_actor_role,
        )
        current_precheck = self.gate_kernel.precheck(
            definition,
            action="current_state",
            actor_role=effective_actor_role,
            execution_state=state,
            runtime_guide=guide,
            subject={
                "project_id": record.get("project_id"),
                "backlog_id": record.get("backlog_id"),
                "contract_execution_id": record.get("contract_execution_id"),
                "parent_contract_execution_id": record.get("parent_contract_execution_id"),
                "root_contract_execution_id": record.get("root_contract_execution_id"),
                "contract_chain_id": record.get("contract_chain_id"),
                "route_token_ref": record.get("route_token_ref"),
            },
        )
        _attach_precheck_decision(guide, current_precheck.to_dict())
        view = deepcopy(dict(record))
        view["completed_lines"] = deepcopy(line_items)
        view["execution_state"] = state
        view["runtime_guide"] = guide
        view["precheck_decision"] = current_precheck.to_dict()
        if sanitized_projection:
            view["completed_lines_projection"] = sanitized_projection
            view["projected_completed_lines_count"] = len(line_items)
        return view

    def submit_line_write(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
        projected_completed_lines: Sequence[Mapping[str, Any]] | None = None,
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.store.get(contract_execution_id)
        definition = self._load_pinned_definition(record)
        effective_write = dict(write)
        body_actor_role = str(effective_write.get("actor_role") or "")
        effective_actor_role = _effective_actor_role(effective_write, actor_role=actor_role)
        if effective_actor_role:
            effective_write["actor_role"] = effective_actor_role
        if body_actor_role and effective_actor_role and body_actor_role != effective_actor_role:
            effective_write["body_actor_role"] = body_actor_role
        _enrich_line_instance_fields(effective_write)
        _enrich_qa_evidence_provenance(effective_write, effective_actor_role)
        guide = self.current_guide(
            contract_execution_id,
            actor_role=effective_actor_role,
        )
        refreshed = self.store.get(contract_execution_id)
        gate_record = refreshed
        if projected_completed_lines is not None:
            gate_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_completed_lines,
                projection=projection,
            )
            guide = gate_record["runtime_guide"]
        gate_decision = self.gate_kernel.precheck(
            definition,
            action="submit_line",
            actor_role=effective_actor_role,
            execution_state=gate_record["execution_state"],
            runtime_guide=guide,
            write=effective_write,
        )
        gate_decision = (
            _direct_fix_worker_graph_context_compat_decision(
                definition=definition,
                record=gate_record,
                write=effective_write,
                gate_decision=gate_decision,
            )
            or gate_decision
        )
        gate_decision = _gate_decision_with_additional_errors(
            gate_decision,
            _mf_parallel_worker_commit_errors(
                gate_record,
                effective_write,
                actor_role=effective_actor_role,
            ),
        )
        if _line_write_declares_no_mutation_expected(effective_write):
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=("line_write_declares_no_mutation_expected",),
                ).to_dict(),
                "precheck_decision": _gate_decision_payload(gate_decision),
                "record": gate_record,
            }
        if not gate_decision.ok:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": _gate_decision_payload(gate_decision),
                "record": gate_record,
            }

        completed_lines = list(refreshed.get("completed_lines") or [])
        written_line = _line_evidence_from_write(effective_write, effective_actor_role)
        completed_lines.append(written_line)
        expected_revision = int(refreshed.get("execution_state_revision") or 1)
        refreshed["completed_lines"] = completed_lines
        refreshed["execution_state_revision"] = expected_revision + 1
        try:
            self.store.update(
                contract_execution_id,
                refreshed,
                expected_revision=expected_revision,
            )
        except ContractRuntimeError as exc:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(ok=False, errors=(str(exc),)).to_dict(),
                "record": self.store.get(contract_execution_id),
            }
        next_guide = self.current_guide(
            contract_execution_id,
            actor_role=effective_actor_role,
        )
        updated = self.store.get(contract_execution_id)
        updated["runtime_guide"] = next_guide
        self.store.update(contract_execution_id, updated)
        result_record = self.store.get(contract_execution_id)
        if projected_completed_lines is not None:
            projected_after_write = list(projected_completed_lines)
            projected_after_write.append(written_line)
            result_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_after_write,
                projection=projection,
            )
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "decision": _gate_decision_payload(gate_decision),
            "record": result_record,
        }

    def precheck_line_write(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
        projected_completed_lines: Sequence[Mapping[str, Any]] | None = None,
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate a proposed line write without appending completed evidence."""

        record = self.store.get(contract_execution_id)
        definition = self._load_pinned_definition(record)
        effective_write = dict(write)
        body_actor_role = str(effective_write.get("actor_role") or "")
        effective_actor_role = _effective_actor_role(effective_write, actor_role=actor_role)
        if effective_actor_role:
            effective_write["actor_role"] = effective_actor_role
        if body_actor_role and effective_actor_role and body_actor_role != effective_actor_role:
            effective_write["body_actor_role"] = body_actor_role
        _enrich_line_instance_fields(effective_write)
        guide = self.current_guide(
            contract_execution_id,
            actor_role=effective_actor_role,
        )
        refreshed = self.store.get(contract_execution_id)
        gate_record = refreshed
        if projected_completed_lines is not None:
            gate_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_completed_lines,
                projection=projection,
            )
            guide = gate_record["runtime_guide"]
        gate_decision = self.gate_kernel.precheck(
            definition,
            action="submit_line",
            actor_role=effective_actor_role,
            execution_state=gate_record["execution_state"],
            runtime_guide=guide,
            write=effective_write,
        )
        gate_decision = (
            _direct_fix_worker_graph_context_compat_decision(
                definition=definition,
                record=gate_record,
                write=effective_write,
                gate_decision=gate_decision,
            )
            or gate_decision
        )
        gate_decision = _gate_decision_with_additional_errors(
            gate_decision,
            _mf_parallel_worker_commit_errors(
                gate_record,
                effective_write,
                actor_role=effective_actor_role,
            ),
        )
        return {
            "schema_version": "contract_runtime_line_write_precheck_result.v1",
            "ok": gate_decision.ok,
            "decision": _gate_decision_payload(gate_decision),
            "record": gate_record,
            "write": effective_write,
            "would_mutate_completed_lines": False,
            "completed_lines_count": len(refreshed.get("completed_lines") or []),
            "projected_completed_lines_count": len(projected_completed_lines or []),
            "execution_state_revision": int(
                refreshed.get("execution_state_revision") or 0
            ),
            "runtime_guide_hash": str(guide.get("runtime_guide_hash") or ""),
        }

    def _load_pinned_definition(self, record: Mapping[str, Any]) -> dict[str, Any]:
        definition = self.registry.get(
            str(record.get("contract_id") or ""),
            version=str(record.get("version") or ""),
            revision=str(record.get("revision") or ""),
            include_deprecated=True,
        )
        _assert_hash(
            "definition_hash",
            record.get("definition_hash"),
            definition.get("definition_hash"),
            record=record,
            definition=definition,
        )
        pinned_source_sha256 = str(record.get("definition_source_sha256") or "")
        if pinned_source_sha256 and not _is_non_runtime_source_mismatch(
            record,
            definition,
        ):
            _assert_hash(
                "definition_source_sha256",
                pinned_source_sha256,
                definition.get("source_sha256"),
                record=record,
                definition=definition,
            )
        return definition


def _is_non_runtime_source_mismatch(
    record: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> bool:
    definition_hash_matches = str(record.get("definition_hash") or "") == str(
        definition.get("definition_hash") or ""
    )
    if not definition_hash_matches:
        return False
    if str(definition.get("status") or "") == "deprecated":
        return True
    pinned_hints_sha256 = str(
        record.get("definition_governance_hints_sha256") or ""
    )
    current_hints_sha256 = str(definition.get("governance_hints_sha256") or "")
    if bool(
        pinned_hints_sha256
        and current_hints_sha256
        and pinned_hints_sha256 != current_hints_sha256
    ):
        return True
    return _is_legacy_root_governance_hints_only_source_mismatch(record, definition)


def _is_legacy_root_governance_hints_only_source_mismatch(
    record: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> bool:
    """Prove an envelope-only transition for records predating its hash field."""

    if str(record.get("definition_governance_hints_sha256") or ""):
        return False
    pinned_source_sha256 = str(record.get("definition_source_sha256") or "")
    current_source_sha256 = str(definition.get("source_sha256") or "")
    current_envelope = definition.get("governance_hints")
    source_path = Path(str(definition.get("_source_path") or ""))
    if not (
        pinned_source_sha256
        and current_source_sha256
        and pinned_source_sha256 != current_source_sha256
        and isinstance(current_envelope, Mapping)
        and source_path.suffix.lower() == ".json"
    ):
        return False

    try:
        current_source = source_path.read_bytes()
        current_payload = json.loads(current_source.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(current_payload, dict):
        return False
    source_envelope = current_payload.get("governance_hints")
    if not isinstance(source_envelope, Mapping) or source_envelope != current_envelope:
        return False
    if _source_bytes_sha256(current_source) != current_source_sha256:
        return False

    current_payload.pop("governance_hints")
    prior_source_candidates = (
        (
            json.dumps(current_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
        (canonical_json(current_payload) + "\n").encode("utf-8"),
    )
    return any(
        _source_bytes_sha256(candidate) == pinned_source_sha256
        for candidate in prior_source_candidates
    )


def _source_bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _enforce_start_precheck(definition: Mapping[str, Any]) -> bool:
    """Hard-block the first migrated source-backed root policy.

    The broader Gate Kernel rollout runs in shadow mode for existing facades so
    contract_add/update/hotfix can be migrated without breaking legacy tests in
    one step. Parallel worker orchestration is already designed as a successor,
    and dogfood exposed it as the unsafe root path, so enforce that one now.
    """

    contract_id = str(definition.get("contract_id") or "")
    aliases = {str(item) for item in definition.get("compat_aliases") or []}
    return (
        contract_id == "mf_parallel"
        or "mf_parallel.v2" in aliases
        or "mf_parallel.v1" in aliases
    )


def _attach_precheck_decision(
    guide: dict[str, Any],
    decision: Mapping[str, Any],
) -> None:
    guide["precheck_decision"] = dict(decision)


def _line_write_declares_no_mutation_expected(write: Mapping[str, Any]) -> bool:
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    candidates = (write, payload)
    for candidate in candidates:
        for key in (
            "dry_run",
            "precheck_only",
            "no_mutation",
            "no_mutation_expected",
            "diagnostic_only",
        ):
            if _truthy_contract_flag(candidate.get(key)):
                return True
        status = str(candidate.get("status") or "").strip().lower()
        if status in {
            "dry_run",
            "dry-run",
            "precheck",
            "precheck_only",
            "diagnostic",
            "diagnostic_probe",
            "diagnostic_probe_no_mutation_expected",
            "no_mutation_expected",
        }:
            return True
        if "no_mutation_expected" in status:
            return True
    return False


def _truthy_contract_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _assert_hash(
    field: str,
    expected: Any,
    actual: Any,
    *,
    record: Mapping[str, Any] | None = None,
    definition: Mapping[str, Any] | None = None,
) -> None:
    if expected != actual:
        if record is not None:
            raise StalePinnedContractExecutionError(
                field,
                expected,
                actual,
                record=record,
                definition=definition,
            )
        raise ContractDefinitionError(f"{field} mismatch for pinned contract execution")


def _record_json(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _decode_record(raw: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        raise ContractRuntimeError("stored contract execution record is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ContractRuntimeError("stored contract execution record must be an object")
    return decoded


def _effective_actor_role(write: Mapping[str, Any], *, actor_role: str | None) -> str:
    for value in (
        actor_role,
        write.get("trusted_actor_role"),
        write.get("derived_actor_role"),
        write.get("actor_role"),
    ):
        token = str(value or "").strip()
        if token:
            return token
    return ""


_LINE_EVIDENCE_OPTIONAL_FIELDS = (
    "line_instance_id",
    "status",
    "verdict",
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "worker_role",
    "lane_id",
    "worker_slot_id",
    "worker_id",
    "payload",
    "verification",
    "artifact_refs",
    "trace_id",
    "graph_trace_id",
    "graph_trace_ids",
    "graph_query_trace_id",
    "graph_query_trace_ids",
    "trace_ids",
    "db_verified",
    "query_source",
    "query_purpose",
    "graph_trace_evidence",
    "target_project_root",
    "commit_sha",
    "head_commit",
    "immutable_head_commit",
    "validated_head_commit",
    "implementation_event_ref",
    "worker_session_id",
    "filer_principal",
    "session_token_ref",
    "fence_token_hash",
    "owned_files",
    "changed_files",
    "commit_diff_files",
    "clean_worktree",
    "dirty_files",
    "actor_session_principal",
    "evidence_owner_actor",
    "evidence_owner_role",
    "evidence_owner_session",
    "evidence_owner_session_ref",
    "submitter_session",
    "submitter_principal",
    "materialized_from",
    "materialized_from_report",
    "authorization_source",
    "observer_impersonation",
    "qa_session_token_ref",
    "parent_materialization_authorized",
    "qa_evidence_provenance",
)
_QA_EVIDENCE_PROVENANCE_FIELDS = (
    "actor_session_principal",
    "evidence_owner_actor",
    "evidence_owner_role",
    "evidence_owner_session",
    "evidence_owner_session_ref",
    "submitter_session",
    "submitter_principal",
    "materialized_from",
    "materialized_from_report",
    "authorization_source",
    "observer_impersonation",
    "qa_session_token_ref",
    "parent_materialization_authorized",
)
_RAW_TOKEN_FIELD_NAMES = {
    "governance_token",
    "governance_tokens",
    "route_token",
    "route_tokens",
    "session_token",
    "session_tokens",
    "token",
    "tokens",
}


def _line_evidence_from_write(
    write: Mapping[str, Any],
    effective_actor_role: str,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "stage_id": str(write.get("stage_id") or ""),
        "line_id": str(write.get("line_id") or ""),
        "actor_role": effective_actor_role,
        "evidence_kind": str(write.get("evidence_kind") or ""),
    }
    for field in _LINE_EVIDENCE_OPTIONAL_FIELDS:
        if field not in write:
            continue
        evidence[field] = _sanitize_line_evidence_value(write.get(field))
    return evidence


def _enrich_qa_evidence_provenance(
    write: dict[str, Any],
    effective_actor_role: str,
) -> None:
    if effective_actor_role != "qa":
        return
    if str(write.get("line_id") or "") != "qa_independent_verification":
        return
    if str(write.get("evidence_kind") or "") != "independent_verification":
        return

    owner_actor = str(
        write.get("evidence_owner_actor")
        or write.get("actor_session_principal")
        or effective_actor_role
    ).strip()
    submitter_principal = str(
        write.get("submitter_principal")
        or write.get("actor_session_principal")
        or owner_actor
        or effective_actor_role
    ).strip()
    write.setdefault("evidence_owner_role", "qa")
    if owner_actor:
        write.setdefault("evidence_owner_actor", owner_actor)
    if submitter_principal:
        write.setdefault("submitter_principal", submitter_principal)
    write.setdefault(
        "authorization_source",
        "qa_session_token_ref" if write.get("qa_session_token_ref") else "qa_role_session",
    )
    write.setdefault("observer_impersonation", False)
    write.setdefault(
        "parent_materialization_authorized",
        bool(
            write.get("submitter_session")
            or write.get("materialized_from")
            or write.get("materialized_from_report")
        ),
    )

    provenance = (
        dict(write.get("qa_evidence_provenance"))
        if isinstance(write.get("qa_evidence_provenance"), Mapping)
        else {}
    )
    for field in _QA_EVIDENCE_PROVENANCE_FIELDS:
        if field in write:
            provenance[field] = write[field]
    provenance.setdefault("evidence_owner_role", "qa")
    if owner_actor:
        provenance.setdefault("evidence_owner_actor", owner_actor)
    if submitter_principal:
        provenance.setdefault("submitter_principal", submitter_principal)
    provenance.setdefault("authorization_source", write.get("authorization_source"))
    provenance.setdefault("observer_impersonation", write.get("observer_impersonation"))
    provenance.setdefault(
        "parent_materialization_authorized",
        write.get("parent_materialization_authorized"),
    )
    write["qa_evidence_provenance"] = provenance


def _enrich_line_instance_fields(write: dict[str, Any]) -> None:
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    for key in (
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "worker_role",
        "lane_id",
        "worker_slot_id",
        "worker_id",
    ):
        if write.get(key):
            continue
        payload_value = payload.get(key)
        if isinstance(payload_value, (str, int, float, bool)):
            text = str(payload_value).strip()
            if text:
                write[key] = text
    if write.get("line_instance_id"):
        return
    line_instance_id = _line_instance_id_from_write(write)
    if line_instance_id:
        write["line_instance_id"] = line_instance_id


def _line_instance_id_from_write(write: Mapping[str, Any]) -> str:
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    runtime_context_id = str(
        write.get("runtime_context_id") or payload.get("runtime_context_id") or ""
    ).strip()
    task_id = str(write.get("task_id") or payload.get("task_id") or "").strip()
    lane_id = str(
        write.get("lane_id")
        or write.get("worker_slot_id")
        or write.get("worker_id")
        or payload.get("lane_id")
        or payload.get("worker_slot_id")
        or payload.get("worker_id")
        or ""
    ).strip()
    if runtime_context_id:
        return f"runtime_context:{runtime_context_id}"
    if task_id:
        return f"task:{task_id}"
    if lane_id:
        return f"lane:{lane_id}"
    return ""


def _attach_writer_role_safe_submit_payload(
    guide: dict[str, Any],
    *,
    definition: Mapping[str, Any],
    record: Mapping[str, Any],
    instruction_bundle: Mapping[str, Any],
    completed_lines: Sequence[Mapping[str, Any]],
    completion_satisfying_lines: Sequence[Mapping[str, Any]],
    sanitized_projection: Mapping[str, Any],
    reader_role: str,
) -> None:
    next_action = guide.get("next_legal_action")
    if not isinstance(next_action, Mapping):
        return
    writer_role = _next_action_writer_role(next_action, fallback_role=reader_role)
    if not writer_role:
        return
    role_hashes = _runtime_guide_role_hashes(
        definition,
        record=record,
        instruction_bundle=instruction_bundle,
        completed_lines=completed_lines,
        completion_satisfying_lines=completion_satisfying_lines,
        sanitized_projection=sanitized_projection,
        reader_role=reader_role,
        writer_role=writer_role,
        next_action=next_action,
        reader_runtime_guide_hash=str(guide.get("runtime_guide_hash") or ""),
    )
    writer_hash = ""
    for item in role_hashes:
        if str(item.get("role") or "") == writer_role:
            writer_hash = str(item.get("runtime_guide_hash") or "")
            break
    attach_writer_role_safe_copy_payload(
        guide,
        reader_role=reader_role,
        writer_role=writer_role,
        writer_runtime_guide_hash=writer_hash or str(guide.get("runtime_guide_hash") or ""),
        role_runtime_guide_hashes=role_hashes,
    )


def _runtime_guide_role_hashes(
    definition: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    instruction_bundle: Mapping[str, Any],
    completed_lines: Sequence[Mapping[str, Any]],
    completion_satisfying_lines: Sequence[Mapping[str, Any]],
    sanitized_projection: Mapping[str, Any],
    reader_role: str,
    writer_role: str,
    next_action: Mapping[str, Any] | None,
    reader_runtime_guide_hash: str,
) -> list[dict[str, str]]:
    role_hashes: list[dict[str, str]] = []
    for role in _known_contract_roles(
        definition,
        next_action=next_action,
        reader_role=reader_role,
        writer_role=writer_role,
    ):
        runtime_guide_hash = reader_runtime_guide_hash if role == reader_role else ""
        if not runtime_guide_hash:
            runtime_guide_hash = _runtime_guide_hash_for_role(
                definition,
                record=record,
                instruction_bundle=instruction_bundle,
                completed_lines=completed_lines,
                completion_satisfying_lines=completion_satisfying_lines,
                sanitized_projection=sanitized_projection,
                actor_role=role,
            )
        role_hashes.append(
            {
                "role": role,
                "runtime_guide_hash": runtime_guide_hash,
                "role_kind": "required_writer" if role == writer_role else "reader_or_known",
            }
        )
    return role_hashes


def _runtime_guide_hash_for_role(
    definition: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    instruction_bundle: Mapping[str, Any],
    completed_lines: Sequence[Mapping[str, Any]],
    completion_satisfying_lines: Sequence[Mapping[str, Any]],
    sanitized_projection: Mapping[str, Any],
    actor_role: str,
) -> str:
    state = build_execution_state(
        definition,
        project_id=str(record["project_id"]),
        backlog_id=str(record["backlog_id"]),
        contract_execution_id=str(record["contract_execution_id"]),
        actor_role=actor_role,
        completed_lines=completion_satisfying_lines,
        route_token_ref=str(record.get("route_token_ref") or ""),
        instruction_bundle_hash=str(record.get("instruction_bundle_hash") or ""),
        execution_state_revision=int(record.get("execution_state_revision") or 1),
    )
    role_guide = compile_runtime_guide(
        definition,
        state,
        instruction_bundle=instruction_bundle,
        judgment_hints=_record_judgment_hints(record),
    )
    _attach_completed_line_evidence(role_guide, completed_lines)
    if sanitized_projection:
        _attach_completed_lines_projection_guidance(
            role_guide,
            sanitized_projection,
        )
    return str(role_guide.get("runtime_guide_hash") or "")


def _known_contract_roles(
    definition: Mapping[str, Any],
    *,
    next_action: Mapping[str, Any] | None,
    reader_role: str,
    writer_role: str,
) -> list[str]:
    roles: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        role = str(value or "").strip()
        if role and role not in seen:
            seen.add(role)
            roles.append(role)

    add(reader_role)
    add(writer_role)
    if isinstance(next_action, Mapping):
        add(next_action.get("owner_role"))
        for role in next_action.get("allowed_writer_roles") or []:
            add(role)
    add(definition.get("role"))
    read_model = definition.get("read_model")
    if isinstance(read_model, Mapping):
        for role in read_model.get("allowed_writer_roles") or []:
            add(role)
        for line in read_model.get("rule_lines") or []:
            if not isinstance(line, Mapping):
                continue
            add(line.get("owner_role"))
            for role in line.get("allowed_writer_roles") or []:
                add(role)
    rule_layer = definition.get("rule_layer")
    stages = rule_layer.get("stages") if isinstance(rule_layer, Mapping) else []
    for stage in stages or []:
        if not isinstance(stage, Mapping):
            continue
        for line in stage.get("lines") or []:
            if not isinstance(line, Mapping):
                continue
            add(line.get("owner_role"))
            for role in line.get("allowed_writer_roles") or []:
                add(role)
    return roles


def _next_action_writer_role(
    next_action: Mapping[str, Any],
    *,
    fallback_role: str,
) -> str:
    owner_role = str(next_action.get("owner_role") or "").strip()
    if owner_role:
        return owner_role
    for role in next_action.get("allowed_writer_roles") or []:
        text = str(role or "").strip()
        if text:
            return text
    return str(fallback_role or "").strip()


def _attach_completed_line_evidence(
    guide: dict[str, Any],
    completed_lines: Any,
) -> None:
    lines = []
    if isinstance(completed_lines, list):
        for item in completed_lines:
            if isinstance(item, Mapping):
                lines.append(_sanitize_completed_line(item))
    guide["completed_lines"] = lines
    guide["runtime_guide_hash"] = stable_sha256(
        {key: value for key, value in guide.items() if key != "runtime_guide_hash"}
    )


def _sanitize_completed_line(line: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for field in (
        "stage_id",
        "line_id",
        "actor_role",
        "evidence_kind",
        *_LINE_EVIDENCE_OPTIONAL_FIELDS,
    ):
        if field not in line:
            continue
        sanitized[field] = _sanitize_line_evidence_value(line.get(field))
    return sanitized


def _sanitize_line_evidence_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if _is_raw_token_field(key_text):
                continue
            result[key_text] = _sanitize_line_evidence_value(child)
        return result
    if isinstance(value, list):
        result_list = []
        for item in value:
            result_list.append(_sanitize_line_evidence_value(item))
        return result_list
    if isinstance(value, tuple):
        return _sanitize_line_evidence_value(list(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _is_raw_token_field(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in _RAW_TOKEN_FIELD_NAMES:
        return True
    return normalized.endswith("_token") and not normalized.endswith("_token_ref")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
