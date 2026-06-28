"""Minimal executable runtime path for config-backed contracts.

This module intentionally stays independent from HTTP, MCP, and timeline
facades. It proves the new contract system can drive the next legal action and
line-level write authorization from source-controlled definitions before legacy
route-context migration begins. Live integrations should use the SQLite store
below so execution state has durable rows and compare-and-swap updates.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .execution_state import build_execution_state
from .gate_kernel import ContractGateKernel
from .guide_compiler import compile_runtime_guide
from .hash import stable_sha256
from .instructions import resolve_instruction_bundle
from .registry import ContractDefinitionRegistry
from .schema import ContractDefinitionError, is_new_execution_allowed
from .write_gate import WriteGateDecision


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
        self.conn.executescript(self.SCHEMA_SQL)
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
MF_PARALLEL_CONTRACT_IDS = frozenset({"mf_parallel", "mf_parallel.v1"})
DIRECT_FIX_QA_EVIDENCE_KINDS = frozenset(
    {"independent_verification", "direct_fix_independent_qa"}
)


def ensure_contract_chain_mapping_schema(conn: sqlite3.Connection) -> None:
    """Create durable backlog-to-contract-chain mapping tables."""

    conn.executescript(CONTRACT_CHAIN_MAPPING_SCHEMA_SQL)


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
    direct_fix = _latest_record(
        [
            record
            for record in records
            if _record_contract_id(record) in DIRECT_FIX_CONTRACT_IDS
        ],
        row_times=row_times,
    )
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
                if _parent_resume_acknowledged(root_record, direct_fix):
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
    repair_line = _find_completed_line(
        child,
        line_ids={"direct_fix_candidate_repair"},
        evidence_kinds={"direct_fix_repair_evidence"},
    )
    return_line = _find_completed_line(
        child,
        line_ids={"direct_fix_return_to_parent"},
        evidence_kinds={"direct_fix_return_to_parent"},
    )
    qa_line = _find_direct_fix_qa_line(
        child,
        generation=generation,
        repair_line=repair_line,
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
) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    for index, line in reversed(list(enumerate(lines))):
        if not isinstance(line, Mapping):
            continue
        line_id = str(line.get("line_id") or "")
        evidence_kind = str(line.get("evidence_kind") or "")
        if line_id in line_ids or evidence_kind in evidence_kinds:
            enriched = dict(line)
            enriched["_completed_line_index"] = index
            return enriched
    return {}


def _find_direct_fix_qa_line(
    record: Mapping[str, Any],
    *,
    generation: int,
    repair_line: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    execution_id = str(record.get("contract_execution_id") or "")
    repair_index = _completed_line_index(repair_line or {})
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
            repair_index=repair_index,
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
    line = _find_parent_resume_ack_line(parent)
    if not line:
        return False
    if str(line.get("actor_role") or "").strip() != "observer":
        return False
    if not _line_status_allows_direct_fix_qa(line):
        return False
    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    if str(payload.get("parent_contract_execution_id") or "").strip() != parent_id:
        return False
    successor_contract_id = str(payload.get("successor_contract_id") or "").strip()
    if successor_contract_id not in DIRECT_FIX_CONTRACT_IDS:
        return False
    successor_execution_id = str(
        payload.get("successor_contract_execution_id") or ""
    ).strip()
    return successor_execution_id == child_id


def _find_parent_resume_ack_line(record: Mapping[str, Any]) -> dict[str, Any]:
    lines = (
        record.get("completed_lines")
        if isinstance(record.get("completed_lines"), list)
        else []
    )
    for index, line in reversed(list(enumerate(lines))):
        if not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "") != "resume_parent_after_successor_return":
            continue
        if str(line.get("evidence_kind") or "") != "successor_return_acknowledgement":
            continue
        enriched = dict(line)
        enriched["_completed_line_index"] = index
        return enriched
    return {}


def _line_status_allows_direct_fix_qa(line: Mapping[str, Any]) -> bool:
    candidates = [line]
    for key in ("payload", "verification", "artifact_refs"):
        value = line.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    for candidate in candidates:
        status = (
            str(candidate.get("status") or candidate.get("verdict") or "")
            .strip()
            .lower()
        )
        if status in {"fail", "failed", "failure", "rejected", "blocked"}:
            return False
    return True


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
) -> dict[str, Any]:
    child_id = str(child.get("contract_execution_id") or "")
    return {
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
        "stage_id": "parent_resume",
        "line_id": "resume_parent_after_successor_return",
        "evidence_kind": "parent_recheck_after_direct_fix_qa",
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


class ContractRuntime:
    """Runtime facade that compiles guides and validates writes from state."""

    def __init__(
        self,
        registry: ContractDefinitionRegistry,
        *,
        instruction_root: str | Path | None = None,
        store: InMemoryContractExecutionStore | SQLiteContractExecutionStore | None = None,
    ) -> None:
        self.registry = registry
        self.instruction_root = Path(instruction_root) if instruction_root is not None else registry.root
        self.store = store or InMemoryContractExecutionStore()
        self.gate_kernel = ContractGateKernel(registry)

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
            "instruction_bundle_hash": instruction_bundle["instruction_bundle_hash"],
            "route_token_ref": route_token_ref,
            "completed_lines": [],
            "execution_state_revision": state["execution_state_revision"],
            "execution_state": state,
            "runtime_guide": guide,
            "precheck_decision": start_precheck.to_dict(),
            "role_binding": dict(role_binding or {}),
            "backlog_lineage": dict(backlog_lineage or {}),
            "metadata": dict(metadata or {}),
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
        state = build_execution_state(
            definition,
            project_id=str(record["project_id"]),
            backlog_id=str(record["backlog_id"]),
            contract_execution_id=contract_execution_id,
            actor_role=actor_role or str(record["execution_state"].get("actor_role") or ""),
            completed_lines=record.get("completed_lines") or [],
            route_token_ref=str(record.get("route_token_ref") or ""),
            instruction_bundle_hash=str(record.get("instruction_bundle_hash") or ""),
            execution_state_revision=int(record.get("execution_state_revision") or 1),
        )
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
        )
        _attach_completed_line_evidence(guide, record.get("completed_lines") or [])
        current_precheck = self.gate_kernel.precheck(
            definition,
            action="current_state",
            actor_role=actor_role or str(record["execution_state"].get("actor_role") or ""),
            execution_state=state,
            runtime_guide=guide,
            subject={
                "project_id": record.get("project_id"),
                "backlog_id": record.get("backlog_id"),
                "contract_execution_id": contract_execution_id,
                "parent_contract_execution_id": record.get("parent_contract_execution_id"),
                "root_contract_execution_id": record.get("root_contract_execution_id"),
                "contract_chain_id": record.get("contract_chain_id"),
                "route_token_ref": record.get("route_token_ref"),
            },
        )
        _attach_precheck_decision(guide, current_precheck.to_dict())
        record["execution_state"] = state
        record["runtime_guide"] = guide
        record["precheck_decision"] = current_precheck.to_dict()
        self.store.update(contract_execution_id, record)
        return guide

    def submit_line_write(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
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
        guide = self.current_guide(
            contract_execution_id,
            actor_role=effective_actor_role,
        )
        refreshed = self.store.get(contract_execution_id)
        gate_decision = self.gate_kernel.precheck(
            definition,
            action="submit_line",
            actor_role=effective_actor_role,
            execution_state=refreshed["execution_state"],
            runtime_guide=guide,
            write=effective_write,
        )
        if _line_write_declares_no_mutation_expected(effective_write):
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=("line_write_declares_no_mutation_expected",),
                ).to_dict(),
                "precheck_decision": gate_decision.to_dict(),
                "record": refreshed,
            }
        if not gate_decision.ok:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": gate_decision.to_dict(),
                "record": refreshed,
            }

        completed_lines = list(refreshed.get("completed_lines") or [])
        completed_lines.append(_line_evidence_from_write(effective_write, effective_actor_role))
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
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "decision": gate_decision.to_dict(),
            "record": self.store.get(contract_execution_id),
        }

    def precheck_line_write(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
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
        gate_decision = self.gate_kernel.precheck(
            definition,
            action="submit_line",
            actor_role=effective_actor_role,
            execution_state=refreshed["execution_state"],
            runtime_guide=guide,
            write=effective_write,
        )
        return {
            "schema_version": "contract_runtime_line_write_precheck_result.v1",
            "ok": gate_decision.ok,
            "decision": gate_decision.to_dict(),
            "record": refreshed,
            "write": effective_write,
            "would_mutate_completed_lines": False,
            "completed_lines_count": len(refreshed.get("completed_lines") or []),
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
        if pinned_source_sha256 and not _is_lifecycle_only_source_mismatch(
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


def _is_lifecycle_only_source_mismatch(
    record: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> bool:
    if str(definition.get("status") or "") != "deprecated":
        return False
    return str(record.get("definition_hash") or "") == str(
        definition.get("definition_hash") or ""
    )


def _enforce_start_precheck(definition: Mapping[str, Any]) -> bool:
    """Hard-block the first migrated source-backed root policy.

    The broader Gate Kernel rollout runs in shadow mode for existing facades so
    contract_add/update/hotfix can be migrated without breaking legacy tests in
    one step. Parallel worker orchestration is already designed as a successor,
    and dogfood exposed it as the unsafe root path, so enforce that one now.
    """

    contract_id = str(definition.get("contract_id") or "")
    aliases = {str(item) for item in definition.get("compat_aliases") or []}
    return contract_id == "mf_parallel" or "mf_parallel.v1" in aliases


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
    "commit_sha",
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
