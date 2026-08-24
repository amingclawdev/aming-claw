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
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from .execution_state import (
    _first_mapping_text,
    _is_mf_parallel_lane_line,
    _iter_worker_payloads,
    _line_instance_id_from_mapping,
    _mf_parallel_worker_instances,
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
_JUDGMENT_HINT_TIMEOUT_SECONDS = 0.2
_JUDGMENT_HINT_CACHE_TTL_SECONDS = 30.0
_JUDGMENT_HINT_CACHE: dict[tuple[str, str], tuple[float, list[Any] | None]] = {}

_MF_PARALLEL_CONTRACT_IDS = frozenset(
    {"mf_parallel", "mf_parallel.v1", "mf_parallel.v2"}
)
_MF_PARALLEL_ATOMIC_DISPATCH_LINE = (
    "dispatch",
    "observer_dispatch_bounded_workers",
)


class ContractRuntimeError(ValueError):
    """Raised when a contract execution cannot be started or advanced."""


class ContractRetirementError(ContractRuntimeError):
    """Typed, source-backed denial for a terminally retired contract."""

    def __init__(self, definition: Mapping[str, Any]) -> None:
        metadata = definition.get("metadata")
        lifecycle = metadata.get("lifecycle") if isinstance(metadata, Mapping) else {}
        result = lifecycle.get("result") if isinstance(lifecycle, Mapping) else {}
        if not isinstance(result, Mapping):
            result = {}
        self.result = deepcopy(dict(result))
        self.code = str(result.get("code") or "contract_terminally_retired")
        self.status = str(result.get("status") or "rejected")
        self.classification = str(
            result.get("classification") or "contract_retirement"
        )
        self.retryable = bool(result.get("retryable", False))
        self.definition = deepcopy(dict(definition))
        message = str(result.get("message") or "Contract is terminally retired")
        super().__init__(f"{self.code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        lifecycle = (self.definition.get("metadata") or {}).get("lifecycle") or {}
        payload = deepcopy(self.result)
        payload.update({
            "schema_version": str(
                self.result.get("schema_version")
                or "contract_runtime_terminal_retirement_error.v1"
            ),
            "code": self.code,
            "error": str(self.result.get("error") or self.code),
            "status": self.status,
            "classification": self.classification,
            "retryable": self.retryable,
            "contract_id": str(self.definition.get("contract_id") or ""),
            "version": str(self.definition.get("version") or ""),
            "revision": str(self.definition.get("revision") or ""),
            "terminal_retirement": True,
            "supersedes_revisions": list(
                lifecycle.get("supersedes_revisions") or []
            ),
        })
        return payload


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

_HISTORICAL_BYPASS_OPERATOR_SUPERSESSION_REF = (
    "backlog:AC-CONTRACT-RUNTIME-BYPASS-TERMINAL-"
    "NO-SOURCE-RESUME-R1-20260723:"
    "chain_trigger_json.historical_loop_rows"
)
_HISTORICAL_BYPASS_OPERATOR_SUPERSESSION_BINDINGS = {
    "AC-CONTRACT-LINE-BYPASS-E0A8A5698E8A27D6": {
        "source_backlog_id": (
            "AC-ACTIVITY-PLAYBACK-PER-RESOURCE-CACHE-"
            "SINGLE-FLIGHT-R2-20260722"
        ),
        "contract_execution_id": "cex-mf-parallel-05dca57d0f222edea88d",
        "stage_id": "observer_integration",
        "line_id": "observer_close_ready",
        "execution_state_revision": 29,
    },
    "AC-CONTRACT-LINE-BYPASS-0FAA24E164C04608": {
        "source_backlog_id": "AC-CONTRACT-LINE-BYPASS-E0A8A5698E8A27D6",
        "contract_execution_id": "cex-mf-parallel-950f36cb45ff712a44d6",
        "stage_id": "observer_integration",
        "line_id": "observer_reconcile",
        "execution_state_revision": 13,
    },
    "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820": {
        "source_backlog_id": "AC-CONTRACT-LINE-BYPASS-E0A8A5698E8A27D6",
        "contract_execution_id": "cex-mf-parallel-950f36cb45ff712a44d6",
        "stage_id": "observer_integration",
        "line_id": "observer_close_ready",
        "execution_state_revision": 14,
    },
    "AC-CONTRACT-LINE-BYPASS-F711E000035BF613": {
        "source_backlog_id": "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
        "contract_execution_id": "cex-mf-parallel-1e5fbd813bda945d383d",
        "stage_id": "observer_integration",
        "bypass_identity_stage_id": "observer_merge",
        "line_id": "observer_merge",
        "execution_state_revision": 18,
    },
    "AC-CONTRACT-LINE-BYPASS-3C0FD2ED41F3AC87": {
        "source_backlog_id": "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
        "contract_execution_id": "cex-mf-parallel-1e5fbd813bda945d383d",
        "stage_id": "qa_graph_context",
        "line_id": "qa_graph_context",
        "execution_state_revision": 10,
    },
    "AC-CONTRACT-LINE-BYPASS-7D856ECDAB165BE2": {
        "source_backlog_id": "AC-CONTRACT-LINE-BYPASS-2CFC4B81B519B820",
        "contract_execution_id": "cex-mf-parallel-1e5fbd813bda945d383d",
        "stage_id": "observer_integration",
        "line_id": "observer_reconcile",
        "execution_state_revision": 19,
    },
}

_BYPASS_RECOVERY_FALLBACK_REQUIRED_SEQUENCE = (
    "independent_root_repair",
    "independent_qa",
    "ordered_batch_merge",
    "current_head_full_reconcile",
    "fresh_generation_from_scenario_1",
)
_BYPASS_RECOVERY_FALLBACK_FORBIDDEN_ACTIONS = (
    "resume_original_contract",
    "return_to_parent",
    "parent_to_resume",
    "retry_source_backlog_close_after_repair",
    "retry_historical_source_backlog_close",
    "repair_downstream_missing_evidence",
    "mark_bypass_source_fixed",
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
CREATE TABLE IF NOT EXISTS worker_implementation_test_results_corrections (
    correction_id              TEXT PRIMARY KEY,
    project_id                 TEXT NOT NULL,
    backlog_id                 TEXT NOT NULL,
    contract_execution_id      TEXT NOT NULL,
    runtime_context_id         TEXT NOT NULL,
    task_id                    TEXT NOT NULL,
    source_completed_line_index INTEGER NOT NULL,
    source_line_instance_id    TEXT NOT NULL,
    source_implementation_lineage_ref TEXT NOT NULL,
    source_line_sha256         TEXT NOT NULL,
    source_execution_state_revision INTEGER NOT NULL,
    source_test_results_sha256 TEXT NOT NULL,
    corrected_test_results_sha256 TEXT NOT NULL,
    source_authority_sha256   TEXT NOT NULL,
    source_worker_id          TEXT NOT NULL,
    source_worker_slot_id     TEXT NOT NULL,
    source_session_token_ref  TEXT NOT NULL,
    source_fence_token_hash   TEXT NOT NULL,
    correction_json            TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    UNIQUE (
        project_id,
        contract_execution_id,
        runtime_context_id,
        task_id,
        source_completed_line_index,
        source_line_sha256
    )
);
CREATE INDEX IF NOT EXISTS idx_worker_implementation_results_correction_source
    ON worker_implementation_test_results_corrections(
        project_id,
        contract_execution_id,
        runtime_context_id,
        task_id
    );
CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_implementation_results_correction_line
    ON worker_implementation_test_results_corrections(
        source_line_sha256,
        source_line_instance_id,
        source_implementation_lineage_ref,
        source_completed_line_index
    );
"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        _ensure_sqlite_schema_without_implicit_commit(
            self.conn,
            self.SCHEMA_SQL,
            required_tables=(
                "contract_runtime_executions",
                "worker_implementation_test_results_corrections",
            ),
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

    def worker_implementation_test_results_corrections(
        self,
        *,
        project_id: str,
        contract_execution_id: str,
        runtime_context_id: str,
        task_id: str,
        source_line_sha256: str = "",
        source_line_instance_id: str = "",
        source_implementation_lineage_ref: str = "",
        source_completed_line_index: int | None = None,
    ) -> list[dict[str, Any]]:
        source_lookup = bool(
            source_line_sha256
            or source_line_instance_id
            or source_implementation_lineage_ref
            or source_completed_line_index is not None
        )
        if source_lookup and not (
            source_line_sha256
            and source_line_instance_id
            and source_implementation_lineage_ref
            and isinstance(source_completed_line_index, int)
            and not isinstance(source_completed_line_index, bool)
            and source_completed_line_index >= 0
        ):
            raise ContractRuntimeError(
                "test-results correction source lookup is incomplete"
            )
        where = (
            "((project_id = ? AND contract_execution_id = ? "
            "AND runtime_context_id = ? AND task_id = ?) "
            "OR source_line_sha256 = ? "
            "OR source_implementation_lineage_ref = ? "
            "OR (source_line_instance_id = ? "
            "AND source_completed_line_index = ?))"
            if source_lookup
            else (
                "project_id = ? AND contract_execution_id = ? "
                "AND runtime_context_id = ? AND task_id = ?"
            )
        )
        params = (
            (
                project_id,
                contract_execution_id,
                runtime_context_id,
                task_id,
                source_line_sha256,
                source_implementation_lineage_ref,
                source_line_instance_id,
                source_completed_line_index,
            )
            if source_lookup
            else (
                project_id,
                contract_execution_id,
                runtime_context_id,
                task_id,
            )
        )
        rows = self.conn.execute(
            f"""
            SELECT
                correction_id,
                project_id,
                backlog_id,
                contract_execution_id,
                runtime_context_id,
                task_id,
                source_completed_line_index,
                source_line_instance_id,
                source_implementation_lineage_ref,
                source_line_sha256,
                source_execution_state_revision,
                source_test_results_sha256,
                corrected_test_results_sha256,
                source_authority_sha256,
                source_worker_id,
                source_worker_slot_id,
                source_session_token_ref,
                source_fence_token_hash,
                created_at,
                correction_json
            FROM worker_implementation_test_results_corrections
            WHERE {where}
            ORDER BY created_at, correction_id
            """,
            params,
        ).fetchall()
        records: list[dict[str, Any]] = []
        columns = (
            "correction_id",
            "project_id",
            "backlog_id",
            "contract_execution_id",
            "runtime_context_id",
            "task_id",
            "source_completed_line_index",
            "source_line_instance_id",
            "source_implementation_lineage_ref",
            "source_line_sha256",
            "source_execution_state_revision",
            "source_test_results_sha256",
            "corrected_test_results_sha256",
            "source_authority_sha256",
            "source_worker_id",
            "source_worker_slot_id",
            "source_session_token_ref",
            "source_fence_token_hash",
            "created_at",
            "correction_json",
        )
        for raw_row in rows:
            row = (
                dict(raw_row)
                if isinstance(raw_row, sqlite3.Row)
                else dict(zip(columns, raw_row, strict=True))
            )
            try:
                decoded = _decode_record(row["correction_json"])
            except (ContractRuntimeError, TypeError, ValueError) as exc:
                raise ContractRuntimeError(
                    "test-results correction durable row is invalid"
                ) from exc
            authority = (
                decoded.get("source_authority")
                if isinstance(decoded.get("source_authority"), Mapping)
                else {}
            )
            duplicated = {
                key: row[key]
                for key in (
                    "correction_id",
                    "project_id",
                    "backlog_id",
                    "contract_execution_id",
                    "runtime_context_id",
                    "task_id",
                    "source_completed_line_index",
                    "source_line_instance_id",
                    "source_implementation_lineage_ref",
                    "source_line_sha256",
                    "source_execution_state_revision",
                    "source_test_results_sha256",
                    "corrected_test_results_sha256",
                    "source_authority_sha256",
                    "created_at",
                )
            }
            authority_duplicates = {
                "source_worker_id": authority.get("worker_id"),
                "source_worker_slot_id": authority.get("worker_slot_id"),
                "source_session_token_ref": authority.get("session_token_ref"),
                "source_fence_token_hash": authority.get("fence_token_hash"),
            }
            mismatch = any(
                type(decoded.get(key)) is not type(expected)
                or decoded.get(key) != expected
                for key, expected in duplicated.items()
            ) or any(
                not isinstance(actual, str) or actual != row[key]
                for key, actual in authority_duplicates.items()
            )
            if mismatch:
                raise ContractRuntimeError(
                    "test-results correction durable row identity mismatch"
                )
            records.append(decoded)
        return records

    def append_worker_implementation_test_results_correction(
        self,
        correction: Mapping[str, Any],
    ) -> dict[str, Any]:
        stored = deepcopy(dict(correction))
        required_text = (
            "correction_id",
            "project_id",
            "backlog_id",
            "contract_execution_id",
            "runtime_context_id",
            "task_id",
            "source_line_instance_id",
            "source_implementation_lineage_ref",
            "source_line_sha256",
            "source_test_results_sha256",
            "corrected_test_results_sha256",
            "source_authority_sha256",
        )
        if any(not str(stored.get(field) or "").strip() for field in required_text):
            raise ContractRuntimeError(
                "test-results correction requires complete immutable identity"
            )
        source_index = stored.get("source_completed_line_index")
        source_revision = stored.get("source_execution_state_revision")
        if (
            not isinstance(source_index, int)
            or isinstance(source_index, bool)
            or source_index < 0
            or not isinstance(source_revision, int)
            or isinstance(source_revision, bool)
            or source_revision <= 0
        ):
            raise ContractRuntimeError(
                "test-results correction source index/revision is invalid"
            )
        created_at = str(
            stored.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        ).strip()
        if not _worker_implementation_canonical_utc_timestamp(created_at):
            raise ContractRuntimeError(
                "test-results correction created_at is invalid"
            )
        stored["created_at"] = created_at
        source_authority = (
            stored.get("source_authority")
            if isinstance(stored.get("source_authority"), Mapping)
            else {}
        )
        source_authority_columns = tuple(
            str(source_authority.get(field) or "").strip()
            for field in (
                "worker_id",
                "worker_slot_id",
                "session_token_ref",
                "fence_token_hash",
            )
        )
        if any(not value for value in source_authority_columns):
            raise ContractRuntimeError(
                "test-results correction source authority is incomplete"
            )
        try:
            self.conn.execute(
                """
                INSERT INTO worker_implementation_test_results_corrections (
                    correction_id,
                    project_id,
                    backlog_id,
                    contract_execution_id,
                    runtime_context_id,
                    task_id,
                    source_completed_line_index,
                    source_line_instance_id,
                    source_implementation_lineage_ref,
                    source_line_sha256,
                    source_execution_state_revision,
                    source_test_results_sha256,
                    corrected_test_results_sha256,
                    source_authority_sha256,
                    source_worker_id,
                    source_worker_slot_id,
                    source_session_token_ref,
                    source_fence_token_hash,
                    correction_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["correction_id"],
                    stored["project_id"],
                    stored["backlog_id"],
                    stored["contract_execution_id"],
                    stored["runtime_context_id"],
                    stored["task_id"],
                    source_index,
                    stored["source_line_instance_id"],
                    stored["source_implementation_lineage_ref"],
                    stored["source_line_sha256"],
                    source_revision,
                    stored["source_test_results_sha256"],
                    stored["corrected_test_results_sha256"],
                    stored["source_authority_sha256"],
                    *source_authority_columns,
                    _record_json(stored),
                    created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContractRuntimeError(
                "test-results correction immutable source already exists"
            ) from exc
        return deepcopy(stored)


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
_SOURCE_CONTRACT_DEFINITION_REGISTRY = ContractDefinitionRegistry()
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
    completed_repair_barrier = current.get(
        "completed_repair_fresh_generation_barrier"
    )
    if isinstance(completed_repair_barrier, Mapping) and completed_repair_barrier:
        active_chain["completed_repair_fresh_generation_barrier"] = dict(
            completed_repair_barrier
        )
    if current.get("terminal") is True:
        terminal_retirement = current.get("terminal_retirement")
        if isinstance(terminal_retirement, Mapping) and terminal_retirement:
            active_chain["terminal_retirement"] = dict(terminal_retirement)
        historical_pinned_identity = current.get("historical_pinned_identity")
        if (
            isinstance(historical_pinned_identity, Mapping)
            and historical_pinned_identity
        ):
            active_chain["historical_pinned_identity"] = dict(
                historical_pinned_identity
            )
        terminal_disposition = current.get("terminal_disposition")
        if isinstance(terminal_disposition, Mapping):
            active_chain["terminal_disposition"] = dict(terminal_disposition)
        recovery_fallback = current.get("bypass_recovery_fallback")
        if isinstance(recovery_fallback, Mapping) and recovery_fallback:
            active_chain["bypass_recovery_fallback"] = dict(recovery_fallback)
    source_refs = _projection_source_refs(chain_records)
    projection_row = {
        "schema_version": "backlog_contract_chain_current.v1",
        "project_id": project_id,
        "backlog_id": backlog_id,
        "contract_chain_id": active_chain_id,
        "root_contract_execution_id": active_chain["root_contract_execution_id"],
        "current_contract_execution_id": current["current_contract_execution_id"],
        "current_contract_id": current["current_contract_id"],
        "parent_to_resume_contract_execution_id": str(
            current.get("parent_to_resume_contract_execution_id") or ""
        ),
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
    projection_hash = contract_chain_projection_hash(projection_row)
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
    projection = _current_projection_from_row(row)
    next_action = (
        projection.get("next_legal_action")
        if isinstance(projection.get("next_legal_action"), Mapping)
        else {}
    )
    completed_repair_barrier = projection.get(
        "completed_repair_fresh_generation_barrier"
    )
    if (
        str(projection.get("readiness_state") or "")
        == "same_row_recovery_target_ready"
        or str(next_action.get("source") or "")
        == "backlog_contract_chain_current.same_row_recovery_cursor"
        or isinstance(next_action.get("same_row_recovery_cursor"), Mapping)
        or (
            not (
                isinstance(completed_repair_barrier, Mapping)
                and completed_repair_barrier
            )
            and _persisted_projection_misses_completed_same_row_recovery(
                conn,
                project_id=project_id,
                backlog_id=backlog_id,
                current_projection=projection,
            )
        )
    ):
        return rebuild_backlog_contract_chain_projection(
            conn,
            project_id=project_id,
            backlog_id=backlog_id,
        )
    return projection


def _persisted_projection_misses_completed_same_row_recovery(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    backlog_id: str,
    current_projection: Mapping[str, Any],
) -> bool:
    """Detect a pre-supersession persisted source projection once.

    Older projections can contain the full source/repair execution set while
    still naming the historical source as current.  Some of those rows use an
    ordinary ``contract_active``/``observer_merge`` shape rather than the
    short-lived recovery-cursor marker, so marker-only migration misses them.
    Recompute only when the durable records independently prove the completed
    same-row repair barrier; ordinary active chains remain read-only.
    """

    current_execution_id = str(
        current_projection.get("current_contract_execution_id") or ""
    ).strip()
    active_chain = (
        current_projection.get("active_chain")
        if isinstance(current_projection.get("active_chain"), Mapping)
        else {}
    )
    execution_ids = {
        str(item or "").strip()
        for item in (active_chain.get("execution_ids") or [])
        if str(item or "").strip()
    }
    active_child_execution_id = str(
        current_projection.get("active_child_contract_execution_id") or ""
    ).strip()
    readiness_state = str(
        current_projection.get("readiness_state") or ""
    ).strip()
    next_action = (
        current_projection.get("next_legal_action")
        if isinstance(current_projection.get("next_legal_action"), Mapping)
        else {}
    )
    next_action_id = str(
        next_action.get("id") or next_action.get("line_id") or ""
    ).strip()
    legacy_source_shape = bool(
        readiness_state == "contract_active"
        and next_action_id == "observer_merge"
        and active_child_execution_id
        and active_child_execution_id == current_execution_id
    )
    completed_child_shape = bool(
        readiness_state == "contract_complete"
        and not next_action
    )
    if (
        not current_execution_id
        or len(execution_ids) < 3
        or not (legacy_source_shape or completed_child_shape)
    ):
        return False
    try:
        rows = conn.execute(
            """
            SELECT record_json, updated_at
            FROM contract_runtime_executions
            WHERE project_id = ? AND backlog_id = ?
            ORDER BY created_at ASC, updated_at ASC, contract_execution_id ASC
            """,
            (project_id, backlog_id),
        ).fetchall()
    except sqlite3.Error:
        return False
    records: list[dict[str, Any]] = []
    row_times: dict[str, str] = {}
    for row in rows:
        raw = row["record_json"] if isinstance(row, sqlite3.Row) else row[0]
        record = _decode_record(raw)
        execution_id = str(
            record.get("contract_execution_id") or ""
        ).strip()
        if not execution_id:
            continue
        records.append(record)
        row_times[execution_id] = str(
            row["updated_at"] if isinstance(row, sqlite3.Row) else row[1]
        )
    recovered = _project_same_row_recovery_state(
        records,
        row_times=row_times,
    )
    recovered_barrier = recovered.get(
        "completed_repair_fresh_generation_barrier"
    )
    recovered_execution_id = str(
        recovered.get("current_contract_execution_id") or ""
    ).strip()
    return bool(
        isinstance(recovered_barrier, Mapping)
        and recovered_barrier
        and recovered_execution_id
        and (
            recovered_execution_id == current_execution_id
            or legacy_source_shape
        )
    )


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
        if str(key)
        not in {
            "projection_hash",
            "projection_watermark",
            "updated_at",
            # These are read-side diagnostics projected from durable fields.
            # They are not part of the persisted projection identity.
            "degraded",
            "projection_source",
        }
    }


def contract_chain_projection_hash(
    projection_row: Mapping[str, Any],
) -> str:
    """Return the canonical hash for a contract-chain projection.

    Minting, server-side overlays, and capsule validation must all use this
    helper.  Keeping the exclusions here prevents read-only projection
    diagnostics from changing the identity of the durable source row.
    """

    return stable_sha256(_projection_hash_payload(projection_row))


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
    same_row_recovery = _project_same_row_recovery_state(
        records,
        row_times=row_times,
    )
    if same_row_recovery:
        return same_row_recovery
    superseded_execution_ids = _recovery_superseded_execution_ids(records)
    selectable_records = [
        record
        for record in records
        if str(record.get("contract_execution_id") or "").strip()
        not in superseded_execution_ids
    ]
    incomplete = [
        record
        for record in selectable_records
        if not _record_is_complete(record)
    ]
    incomplete_children = [
        record
        for record in incomplete
        if str(record.get("parent_contract_execution_id") or "").strip()
    ]
    current_record = _latest_record(
        incomplete_children or incomplete or selectable_records or records,
        row_times=row_times,
    )
    if not current_record:
        return _empty_projected_state("missing_contract_runtime_execution")
    return _project_record_state(current_record)


def _same_row_recovery_source_execution_id(
    record: Mapping[str, Any],
) -> str:
    values: set[str] = set()
    for lineage_field in ("metadata", "backlog_lineage"):
        lineage = record.get(lineage_field)
        if not isinstance(lineage, Mapping):
            continue
        for field in (
            "stale_contract_execution_id",
            "source_contract_execution_id",
        ):
            value = str(lineage.get(field) or "").strip()
            if value:
                values.add(value)
    return next(iter(values)) if len(values) == 1 else ""


def _same_row_recovery_target(
    parent: Mapping[str, Any],
    child: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Resolve only an immutable observer-merge target for one exact parent."""

    parent_id = str(parent.get("contract_execution_id") or "").strip()
    child_id = str(child.get("contract_execution_id") or "").strip()
    runtime_guide = (
        parent.get("runtime_guide")
        if isinstance(parent.get("runtime_guide"), Mapping)
        else {}
    )
    next_action = (
        runtime_guide.get("next_legal_action")
        if isinstance(runtime_guide.get("next_legal_action"), Mapping)
        else {}
    )
    allowed_writer_roles = list(next_action.get("allowed_writer_roles") or [])
    try:
        source_revision = int(parent.get("execution_state_revision") or 0)
    except (TypeError, ValueError):
        return {}, ""
    source_guide_hash = str(
        runtime_guide.get("runtime_guide_hash") or ""
    ).strip()
    if not (
        parent_id
        and child_id
        and child_id != parent_id
        and source_revision > 0
        and source_guide_hash
        and str(next_action.get("stage_id") or "") == "observer_integration"
        and str(next_action.get("line_id") or "") == "observer_merge"
        and str(next_action.get("evidence_kind") or "") == "merge"
        and str(next_action.get("owner_role") or "") == "observer"
        and "observer" in allowed_writer_roles
    ):
        return {}, ""

    canonical_target = {
        "schema_version": "contract_runtime.same_row_recovery_target.v1",
        "source_of_authority": (
            "stale_contract_runtime.runtime_guide.next_legal_action"
        ),
        "source_contract_execution_id": parent_id,
        "source_execution_state_revision": source_revision,
        "source_runtime_guide_hash": source_guide_hash,
        "stage_id": "observer_integration",
        "line_id": "observer_merge",
        "action": str(next_action.get("action") or "").strip()
        or "record_merge",
        "evidence_kind": "merge",
        "owner_role": "observer",
        "allowed_writer_roles": allowed_writer_roles,
        "historical_parent_immutable": True,
        "authoritative_pass_synthesized": False,
    }
    canonical_target["target_hash"] = stable_sha256(canonical_target)

    frozen_targets: list[dict[str, Any]] = []
    for lineage_field in ("metadata", "backlog_lineage"):
        lineage = child.get(lineage_field)
        if not isinstance(lineage, Mapping):
            continue
        target = lineage.get("current_repair_target")
        if isinstance(target, Mapping):
            frozen_targets.append(dict(target))
    if frozen_targets:
        if (
            len(frozen_targets) != 2
            or stable_sha256(frozen_targets[0])
            != stable_sha256(frozen_targets[1])
            or frozen_targets[0] != canonical_target
        ):
            return {}, ""
        return canonical_target, "frozen_recovery_target"

    metadata = (
        child.get("metadata")
        if isinstance(child.get("metadata"), Mapping)
        else {}
    )
    if not (
        str(metadata.get("source_contract_execution_id") or "").strip()
        == parent_id
        and str(metadata.get("source_failed_qa_event_ref") or "").startswith(
            "timeline:"
        )
        and str(metadata.get("repair_run_id") or "").strip()
        and re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}",
            str(metadata.get("immutable_checkpoint_commit") or "")
            .strip()
            .lower(),
        )
        and re.fullmatch(
            r"timeline:\d+",
            str(metadata.get("immutable_checkpoint_reconcile_ref") or "")
            .strip(),
        )
        and metadata.get("authoritative_pass_synthesized") is False
    ):
        return {}, ""
    return canonical_target, "legacy_persisted_parent_guide"


def _same_row_recovery_lineage_matches(
    parent: Mapping[str, Any],
    child: Mapping[str, Any],
) -> bool:
    parent_id = str(parent.get("contract_execution_id") or "").strip()
    child_id = str(child.get("contract_execution_id") or "").strip()
    if _same_row_recovery_source_execution_id(child) != parent_id:
        return False
    recovery_ids = {
        str(lineage.get("recovery_contract_execution_id") or "").strip()
        for lineage_field in ("metadata", "backlog_lineage")
        if isinstance((lineage := child.get(lineage_field)), Mapping)
        and str(lineage.get("recovery_contract_execution_id") or "").strip()
    }
    if recovery_ids and recovery_ids != {child_id}:
        return False
    metadata = (
        child.get("metadata")
        if isinstance(child.get("metadata"), Mapping)
        else {}
    )
    if not (
        metadata.get("historical_evidence_replayed") is False
        and metadata.get("authoritative_pass_synthesized") is False
    ):
        return False
    for field in (
        "project_id",
        "backlog_id",
        "parent_contract_execution_id",
        "root_contract_execution_id",
        "contract_chain_id",
    ):
        if str(child.get(field) or "").strip() != str(
            parent.get(field) or ""
        ).strip():
            return False
    return _record_contract_id(child) == _record_contract_id(parent)


def _same_row_recovery_completion_authority(
    child: Mapping[str, Any],
) -> dict[str, Any]:
    if not _record_is_complete(child):
        return {}
    project_id = str(child.get("project_id") or "").strip()
    backlog_id = str(child.get("backlog_id") or "").strip()
    child_id = str(child.get("contract_execution_id") or "").strip()
    child_metadata = (
        child.get("metadata")
        if isinstance(child.get("metadata"), Mapping)
        else {}
    )
    expected_merge_parent_task_ids = {
        value
        for value in (
            child_id,
            str(child_metadata.get("repair_run_id") or "").strip(),
        )
        if value
    }
    lines = (
        child.get("completed_lines")
        if isinstance(child.get("completed_lines"), list)
        else []
    )
    qa_index = -1
    merge_index = -1
    reconcile_index = -1
    close_index = -1
    merge_authority: Mapping[str, Any] = {}
    reconcile_authority: Mapping[str, Any] = {}
    current_full: Mapping[str, Any] = {}
    for index, line in enumerate(lines):
        if not isinstance(line, Mapping):
            continue
        line_id = str(line.get("line_id") or "").strip()
        if line_id == "qa_independent_verification":
            provenance = (
                line.get("qa_evidence_provenance")
                if isinstance(line.get("qa_evidence_provenance"), Mapping)
                else {}
            )
            binding = (
                provenance.get("authenticated_qa_binding")
                if isinstance(
                    provenance.get("authenticated_qa_binding"), Mapping
                )
                else {}
            )
            status_gate = (
                provenance.get("completion_status_gate")
                if isinstance(provenance.get("completion_status_gate"), Mapping)
                else {}
            )
            if (
                str(line.get("actor_role") or "") == "qa"
                and str(line.get("evidence_kind") or "")
                == "independent_verification"
                and _line_status_allows_contract_completion(
                    line,
                    source_record=child,
                    source_line_index=index,
                )
                and str(provenance.get("schema_version") or "")
                == "qa_evidence_provenance.v1"
                and provenance.get("server_derived") is True
                and str(provenance.get("evidence_owner_role") or "") == "qa"
                and provenance.get("observer_impersonation") is False
                and str(binding.get("schema_version") or "")
                == "contract_runtime.authenticated_qa_binding.v1"
                and binding.get("server_derived") is True
                and binding.get("independent_verification_session_matched")
                is True
                and str(binding.get("qa_principal") or "").strip()
                and str(binding.get("qa_session_id") or "").strip()
                and str(status_gate.get("schema_version") or "")
                == "contract_runtime.qa_completion_status_gate.v1"
                and status_gate.get("server_derived") is True
                and status_gate.get("top_level_status_present") is True
                and status_gate.get("top_level_status_passing") is True
            ):
                qa_index = index
            continue
        if line_id == "observer_merge" and qa_index >= 0 and index > qa_index:
            payload = (
                line.get("payload")
                if isinstance(line.get("payload"), Mapping)
                else {}
            )
            durable = (
                payload.get("durable_merge_authority")
                if isinstance(payload.get("durable_merge_authority"), Mapping)
                else {}
            )
            branch_head = str(durable.get("branch_head") or "").strip().lower()
            merge_commit = str(durable.get("merge_commit") or "").strip().lower()
            try:
                authority_qa_index = int(
                    durable.get("qa_completed_line_index", -1)
                )
            except (TypeError, ValueError):
                authority_qa_index = -1
            if (
                str(line.get("actor_role") or "") == "observer"
                and str(line.get("evidence_kind") or "") == "merge"
                and _line_status_allows_contract_completion(line)
                and str(durable.get("schema_version") or "")
                == "contract_runtime.observer_merge_durable_authority.v1"
                and durable.get("server_derived") is True
                and durable.get("db_verified") is True
                and durable.get("merge_gate_passed") is True
                and durable.get("qa_contract_runtime_verified") is True
                and durable.get("no_pass_claim") is False
                and durable.get("authoritative_pass_synthesized") is False
                and durable.get("close_satisfying") is True
                and str(durable.get("project_id") or "") == project_id
                and str(durable.get("backlog_id") or "") == backlog_id
                and str(durable.get("parent_task_id") or "")
                in expected_merge_parent_task_ids
                and authority_qa_index == qa_index
                and str(durable.get("qa_acceptance_ref") or "").strip()
                and str(durable.get("queue_item_status") or "") == "merged"
                and str(durable.get("merge_event_ref") or "").startswith(
                    "timeline:"
                )
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", branch_head)
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge_commit)
                and str(durable.get("target_head_after_merge") or "")
                .strip()
                .lower()
                == merge_commit
                and str(line.get("commit_sha") or "").strip().lower()
                == merge_commit
            ):
                merge_index = index
                merge_authority = durable
            continue
        if (
            line_id == "observer_reconcile"
            and merge_index >= 0
            and index > merge_index
        ):
            payload = (
                line.get("payload")
                if isinstance(line.get("payload"), Mapping)
                else {}
            )
            wrapper = (
                payload.get("reconcile_authority")
                if isinstance(payload.get("reconcile_authority"), Mapping)
                else {}
            )
            canonical = _canonical_current_full_reconcile_activation(
                wrapper,
                expected_merge_source_ref=str(
                    merge_authority.get("merge_event_ref") or ""
                ),
                expected_merged_commit=str(
                    merge_authority.get("merge_commit") or ""
                ),
                expected_project_id=project_id,
                expected_backlog_id=backlog_id,
                expected_contract_execution_id=child_id,
            )
            if (
                str(line.get("actor_role") or "") == "observer"
                and str(line.get("evidence_kind") or "") == "reconcile"
                and _line_status_allows_contract_completion(line)
                and str(wrapper.get("schema_version") or "")
                == "contract_runtime.observer_reconcile_record_authority.v1"
                and wrapper.get("server_derived") is True
                and wrapper.get("record_verified") is True
                and wrapper.get("merge_projection_verified") is True
                and wrapper.get("dispatch_lineage_verified") is True
                and wrapper.get("reconcile_event_recorded") is True
                and wrapper.get(
                    "current_full_reconcile_activation_verified"
                )
                is True
                and str(wrapper.get("merge_source_ref") or "")
                == str(merge_authority.get("merge_event_ref") or "")
                and str(wrapper.get("merged_commit_sha") or "").lower()
                == str(merge_authority.get("merge_commit") or "").lower()
                and str(wrapper.get("authority_hash") or "")
                == stable_sha256(
                    {
                        key: value
                        for key, value in wrapper.items()
                        if key != "authority_hash"
                    }
                )
                and canonical
            ):
                reconcile_index = index
                reconcile_authority = wrapper
                current_full = canonical
            continue
        if (
            line_id == "observer_close_ready"
            and reconcile_index >= 0
            and index > reconcile_index
            and str(line.get("actor_role") or "") == "observer"
            and str(line.get("evidence_kind") or "") == "close_ready"
            and _line_status_allows_contract_completion(line)
            and str(line.get("commit_sha") or "").strip().lower()
            == str(merge_authority.get("merge_commit") or "").strip().lower()
        ):
            close_index = index

    merge_event_ref = str(merge_authority.get("merge_event_ref") or "")
    reconcile_source_ref = str(
        reconcile_authority.get("reconcile_source_ref")
        or current_full.get("reconcile_source_ref")
        or ""
    )
    merge_match = re.fullmatch(r"timeline:(\d+)", merge_event_ref)
    reconcile_match = re.fullmatch(r"timeline:(\d+)", reconcile_source_ref)
    if not (
        0 <= qa_index < merge_index < reconcile_index < close_index
        and merge_match
        and reconcile_match
        and int(merge_match.group(1)) < int(reconcile_match.group(1))
    ):
        return {}
    return {
        "qa_completed_line_index": qa_index,
        "merge_completed_line_index": merge_index,
        "reconcile_completed_line_index": reconcile_index,
        "close_completed_line_index": close_index,
        "merge_event_ref": merge_event_ref,
        "reconcile_source_ref": reconcile_source_ref,
        "merged_commit_sha": str(merge_authority.get("merge_commit") or ""),
        "active_snapshot_id": str(current_full.get("active_snapshot_id") or ""),
    }


def _project_same_row_recovery_state(
    records: list[dict[str, Any]],
    *,
    row_times: Mapping[str, str],
) -> dict[str, Any]:
    records_by_id = {
        str(record.get("contract_execution_id") or "").strip(): record
        for record in records
        if str(record.get("contract_execution_id") or "").strip()
    }
    candidates: list[
        tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            str,
        ]
    ] = []
    for child in records:
        source_id = _same_row_recovery_source_execution_id(child)
        parent = records_by_id.get(source_id)
        if not parent or not _same_row_recovery_lineage_matches(parent, child):
            continue
        target, target_source = _same_row_recovery_target(parent, child)
        completion = _same_row_recovery_completion_authority(child)
        if not target or not completion:
            continue
        candidates.append(
            (parent, child, target, completion, target_source)
        )
    if not candidates:
        return {}
    candidates.sort(
        key=lambda item: _record_order_key(item[1], row_times=row_times)
    )
    parent, child, target, completion, target_source = candidates[-1]
    parent_id = str(parent.get("contract_execution_id") or "")
    child_id = str(child.get("contract_execution_id") or "")
    if any(
        not _record_is_complete(record)
        and str(record.get("contract_execution_id") or "") != parent_id
        and str(record.get("parent_contract_execution_id") or "").strip()
        for record in records
    ):
        return {}

    barrier = {
        "schema_version": (
            "contract_runtime.completed_repair_fresh_generation_barrier.v1"
        ),
        "status": "repair_complete_fresh_generation_required",
        "source": "completed_recovery_child_authority",
        "target_source": target_source,
        "repair_child_contract_execution_id": child_id,
        "historical_source_contract_execution_id": parent_id,
        "repair_target_hash": str(target.get("target_hash") or ""),
        "source_execution_state_revision": int(
            target.get("source_execution_state_revision") or 0
        ),
        "source_runtime_guide_hash": str(
            target.get("source_runtime_guide_hash") or ""
        ),
        **completion,
        "historical_parent_mutated": False,
        "historical_completed_lines_mutated": False,
        "historical_missing_evidence_backfilled": False,
        "historical_source_scheduler_eligible": False,
        "historical_source_resume_eligible": False,
        "authoritative_pass_synthesized": False,
        "current_generation_disposition": "discarded",
        "fresh_generation_required": True,
        "fresh_generation_start": "scenario_1",
        "diagnostic_fixed_eligible": False,
        "advisory_only": True,
        "authorizes_write": False,
        "authorizes_pass": False,
        "satisfies_gate": False,
        "mutates_runtime_state": False,
        "required_sequence": ["fresh_generation_from_scenario_1"],
        "forbidden_actions": list(
            _BYPASS_RECOVERY_FALLBACK_FORBIDDEN_ACTIONS
        ),
        "prompt": (
            "The bounded repair completed independent QA, ordered merge, and "
            "current-HEAD full reconcile. Do not return to the historical "
            "source or fill any historical missing line. Start a fresh "
            "validation generation from scenario 1."
        ),
    }
    barrier["barrier_hash"] = stable_sha256(barrier)
    return {
        "current_contract_execution_id": child_id,
        "current_contract_id": _record_contract_id(child),
        "parent_to_resume_contract_execution_id": "",
        "active_child_contract_execution_id": "",
        "readiness_state": "contract_complete",
        "generation": int(child.get("execution_state_revision") or 0),
        "next_legal_action": {},
        "scheduler_eligible": False,
        "schedulable": False,
        "resume_eligible": False,
        "resumable": False,
        "completed_repair_fresh_generation_barrier": barrier,
    }


def _recovery_superseded_execution_ids(
    records: list[dict[str, Any]],
) -> set[str]:
    execution_ids = {
        str(record.get("contract_execution_id") or "").strip()
        for record in records
        if str(record.get("contract_execution_id") or "").strip()
    }
    superseded: set[str] = set()
    for record in records:
        recovery_execution_id = str(
            record.get("contract_execution_id") or ""
        ).strip()
        for lineage_field in ("metadata", "backlog_lineage"):
            lineage = record.get(lineage_field)
            if not isinstance(lineage, Mapping):
                continue
            stale_execution_id = str(
                lineage.get("stale_contract_execution_id") or ""
            ).strip()
            if (
                stale_execution_id
                and stale_execution_id != recovery_execution_id
                and stale_execution_id in execution_ids
            ):
                superseded.add(stale_execution_id)
    return superseded


def _project_record_state(record: Mapping[str, Any]) -> dict[str, Any]:
    terminal_retirement = _terminal_retirement_record_projection(record)
    if terminal_retirement is not None:
        return terminal_retirement
    terminal_supersession = terminal_supersession_receipt_for_record(record)
    if terminal_supersession:
        source_execution_id = str(
            record.get("contract_execution_id") or ""
        )
        fresh_execution_id = str(
            terminal_supersession.get("fresh_contract_execution_id") or ""
        )
        return {
            # This is the projection of the terminal source record.  Preserve
            # its custody identity here; the chain-level projection selects
            # the fresh execution independently.
            "current_contract_execution_id": source_execution_id,
            "current_contract_id": _record_contract_id(record),
            "parent_to_resume_contract_execution_id": "",
            "active_child_contract_execution_id": "",
            "readiness_state": "superseded_no_pass",
            "disposition": "superseded_no_pass",
            "terminal": True,
            "scheduler_eligible": False,
            "schedulable": False,
            "current_eligible": False,
            "close_eligible": False,
            "closeable": False,
            "resume_eligible": False,
            "resumable": False,
            "generation": int(record.get("execution_state_revision") or 0),
            "next_legal_action": {},
            "next_legal_execution_id": fresh_execution_id,
            "terminal_disposition": {
                "schema_version": (
                    "contract_runtime.terminal_supersession_disposition.v1"
                ),
                "status": "superseded_no_pass",
                "terminal": True,
                "no_pass_claim": True,
                "pass_synthesized": False,
                "receipt": terminal_supersession,
            },
        }
    current_id = str(record.get("contract_execution_id") or "")
    current_contract_id = _record_contract_id(record)
    terminal = _audited_bypass_terminal_disposition(record)
    if terminal:
        projected = {
            "current_contract_execution_id": "",
            "current_contract_id": "",
            "active_child_contract_execution_id": "",
            "readiness_state": "completed_with_exception",
            "disposition": "completed_with_exception",
            "row_status": "WAIVED",
            "source_row_status": "WAIVED",
            "terminal": True,
            "scheduler_eligible": False,
            "schedulable": False,
            "current_eligible": False,
            "close_eligible": False,
            "closeable": False,
            "resume_eligible": False,
            "resumable": False,
            "generation": int(record.get("execution_state_revision") or 0),
            "next_legal_action": {},
            "terminal_disposition": terminal,
        }
        recovery_fallback = terminal.get("bypass_recovery_fallback")
        if isinstance(recovery_fallback, Mapping) and recovery_fallback:
            projected["bypass_recovery_fallback"] = dict(recovery_fallback)
        return projected
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


def terminal_supersession_receipt_for_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one server-derived immutable no-PASS supersession receipt."""

    metadata = (
        record.get("metadata")
        if isinstance(record.get("metadata"), Mapping)
        else {}
    )
    receipt = (
        metadata.get("terminal_supersession_receipt")
        if isinstance(
            metadata.get("terminal_supersession_receipt"), Mapping
        )
        else {}
    )
    if not receipt:
        return {}
    receipt_hash = str(receipt.get("receipt_hash") or "").strip()
    core = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_hash", "receipt_ref"}
    }
    source_execution_id = str(
        receipt.get("source_contract_execution_id") or ""
    ).strip()
    fresh_execution_id = str(
        receipt.get("fresh_contract_execution_id") or ""
    ).strip()
    if not (
        receipt.get("schema_version")
        == "mf_parallel.terminal_supersession_receipt.v1"
        and receipt.get("server_derived") is True
        and receipt.get("no_pass_claim") is True
        and receipt.get("authoritative_pass_synthesized") is False
        and receipt.get("old_execution_terminal") is True
        and source_execution_id
        and source_execution_id
        == str(record.get("contract_execution_id") or "").strip()
        and fresh_execution_id
        and fresh_execution_id != source_execution_id
        and receipt_hash
        and receipt_hash == stable_sha256(core)
    ):
        return {}
    return deepcopy(dict(receipt))


def _project_direct_fix_state(
    child: Mapping[str, Any],
    *,
    root_record: Mapping[str, Any],
) -> dict[str, Any]:
    terminal_retirement = _terminal_retirement_record_projection(child)
    if terminal_retirement is not None:
        return terminal_retirement
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


def _source_terminal_retirement_for_record(
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    contract_id = _record_contract_id(record)
    version = str(record.get("version") or "").strip()
    if not contract_id or not version:
        return None
    return _SOURCE_CONTRACT_DEFINITION_REGISTRY.terminal_retirement_for(
        contract_id,
        version=version,
    )


def _terminal_retirement_record_projection(
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    retirement = _source_terminal_retirement_for_record(record)
    if retirement is None:
        return None
    execution_id = str(record.get("contract_execution_id") or "").strip()
    version = str(record.get("version") or "").strip()
    retirement_result = ContractRetirementError(retirement).to_dict()
    return {
        "current_contract_execution_id": execution_id,
        "current_contract_id": _record_contract_id(record),
        "parent_to_resume_contract_execution_id": "",
        "active_child_contract_execution_id": "",
        "readiness_state": "terminal_retired",
        "disposition": "terminal_retired",
        "terminal": True,
        "historical_pinned_read_only": True,
        "scheduler_eligible": False,
        "schedulable": False,
        "current_eligible": False,
        "close_eligible": False,
        "closeable": False,
        "resume_eligible": False,
        "resumable": False,
        "retry_eligible": False,
        "write_eligible": False,
        "generation": int(record.get("execution_state_revision") or 0),
        "next_legal_action": {},
        "terminal_retirement": retirement_result,
        "historical_pinned_identity": {
            "contract_execution_id": execution_id,
            "contract_id": _record_contract_id(record),
            "version": version,
            "revision": str(record.get("revision") or ""),
            "definition_hash": str(record.get("definition_hash") or ""),
            "definition_source_sha256": str(
                record.get("definition_source_sha256") or ""
            ),
        },
    }


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


def _bind_authoritative_common_rule_join_to_state(
    state: dict[str, Any],
    join: Mapping[str, Any],
) -> None:
    """Bind the exact Rule authority set before execution-state hashing."""

    if join.get("authoritative") is not True:
        return
    state["authoritative_common_rule_join"] = deepcopy(dict(join))
    state["execution_state_hash"] = stable_sha256(
        {
            key: value
            for key, value in state.items()
            if key != "execution_state_hash"
        }
    )


def _bind_authoritative_common_rule_join_to_guide(
    guide: dict[str, Any],
    join: Mapping[str, Any],
) -> None:
    """Expose the same join to every precheck/write Guide projection."""

    if join.get("authoritative") is not True:
        return
    guide["authoritative_common_rule_join"] = deepcopy(dict(join))
    guide["runtime_guide_hash"] = stable_sha256(
        {
            key: value
            for key, value in guide.items()
            if key != "runtime_guide_hash"
        }
    )


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


def _canonical_audited_bypass_payload(
    line: Mapping[str, Any],
) -> Mapping[str, Any]:
    payload = (
        line.get("payload")
        if isinstance(line.get("payload"), Mapping)
        else {}
    )
    if not (
        str(line.get("evidence_kind") or "") == "contract_line_bypass"
        and str(line.get("status") or "").lower() == "waived"
        and line.get("no_pass_claim") is True
        and str(payload.get("schema_version") or "")
        == "contract_line_bypass.v1"
        and payload.get("no_pass_claim") is True
        and str(payload.get("disposition") or "")
        == "proceeded_with_exception"
    ):
        return {}
    return payload


def _contract_runtime_no_pass_generation(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the immutable no-PASS generation rooted at the first bypass.

    A bypass taints the source execution generation without turning any gate
    into PASS.  Later inherited gate exceptions reuse this root diagnostic;
    they never create a new diagnostic merely because upstream PASS evidence
    is unavailable.  The completed-line ledger is the authority so legacy v1
    roots acquire the same deterministic generation identity without rewrite.
    """

    execution_id = str(record.get("contract_execution_id") or "").strip()
    source_backlog_id = str(record.get("backlog_id") or "").strip()
    if not execution_id or not source_backlog_id:
        return {}
    completed = record.get("completed_lines")
    if not isinstance(completed, Sequence):
        return {}
    for line_index, line in enumerate(completed):
        if not isinstance(line, Mapping):
            continue
        payload = _canonical_audited_bypass_payload(line)
        if not payload:
            continue
        bypass_identity = str(payload.get("bypass_identity") or "").strip()
        diagnostic_id = str(
            payload.get("diagnostic_backlog_id") or ""
        ).strip()
        if not bypass_identity or not diagnostic_id:
            continue
        persisted_generation = (
            payload.get("no_pass_generation")
            if isinstance(payload.get("no_pass_generation"), Mapping)
            else {}
        )
        generation_id = str(
            persisted_generation.get("generation_id") or ""
        ).strip()
        if not generation_id:
            digest = hashlib.sha256(
                canonical_json(
                    {
                        "project_id": str(record.get("project_id") or ""),
                        "source_backlog_id": source_backlog_id,
                        "contract_execution_id": execution_id,
                        "root_bypass_identity": bypass_identity,
                        "root_line_instance_id": str(
                            line.get("line_instance_id") or ""
                        ),
                    }
                ).encode("utf-8")
            ).hexdigest()[:20]
            generation_id = f"bypassgen-{digest}"
        inherited_count = 0
        for candidate in completed[line_index + 1 :]:
            if not isinstance(candidate, Mapping):
                continue
            candidate_payload = _canonical_audited_bypass_payload(candidate)
            candidate_generation = (
                candidate_payload.get("no_pass_generation")
                if isinstance(
                    candidate_payload.get("no_pass_generation"), Mapping
                )
                else {}
            )
            if (
                str(candidate_generation.get("generation_id") or "").strip()
                == generation_id
                and str(candidate_generation.get("role") or "").strip()
                == "inherited_gate"
            ):
                inherited_count += 1
        return {
            "schema_version": "contract_runtime.no_pass_generation.v1",
            "generation_id": generation_id,
            "status": "active_no_pass",
            "source_backlog_id": source_backlog_id,
            "contract_execution_id": execution_id,
            "root_completed_line_index": line_index,
            "root_generation_persisted": bool(persisted_generation),
            "root_bypass_identity": bypass_identity,
            "root_diagnostic_backlog_id": diagnostic_id,
            "root_stage_id": str(line.get("stage_id") or ""),
            "root_line_id": str(line.get("line_id") or ""),
            "root_line_instance_id": str(line.get("line_instance_id") or ""),
            "root_classification": str(
                payload.get("classification") or ""
            ),
            "root_execution_state_revision": int(
                payload.get("execution_state_revision") or 0
            ),
            "root_reason": str(payload.get("reason") or ""),
            "inherited_gate_count": inherited_count,
            "no_pass_claim": True,
            "authoritative_pass_synthesized": False,
            "unlock_policy": (
                "authoritative_root_repair_then_fresh_execution_generation"
            ),
            "historical_lines_append_only": True,
        }
    return {}


def _historical_audited_bypass_supersession_disposition(
    record: Mapping[str, Any],
    canonical_bypasses: Sequence[tuple[int, Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    """Terminalize only the operator-named immutable historical bypass loops."""

    source_backlog_id = str(record.get("backlog_id") or "").strip()
    contract_execution_id = str(
        record.get("contract_execution_id") or ""
    ).strip()
    if not source_backlog_id or not contract_execution_id:
        return {}

    matched_bindings: list[dict[str, Any]] = []
    for line_index, line, payload in canonical_bypasses:
        diagnostic_backlog_id = str(
            payload.get("diagnostic_backlog_id") or ""
        ).strip()
        binding = _HISTORICAL_BYPASS_OPERATOR_SUPERSESSION_BINDINGS.get(
            diagnostic_backlog_id
        )
        if not isinstance(binding, Mapping):
            continue
        expected_revision = int(
            binding.get("execution_state_revision") or 0
        )
        try:
            evidence_revision = int(
                payload.get("execution_state_revision") or 0
            )
        except (TypeError, ValueError):
            evidence_revision = -1
        identity_stage_id = str(
            binding.get("bypass_identity_stage_id")
            or binding.get("stage_id")
            or ""
        )
        expected_identity = (
            f"bypass:{contract_execution_id}:revision-{expected_revision}:"
            f"{identity_stage_id}:{binding.get('line_id')}"
        )
        if not (
            source_backlog_id
            == str(binding.get("source_backlog_id") or "")
            and contract_execution_id
            == str(binding.get("contract_execution_id") or "")
            and str(line.get("stage_id") or "")
            == str(binding.get("stage_id") or "")
            and str(line.get("line_id") or "")
            == str(binding.get("line_id") or "")
            and evidence_revision == expected_revision
            and str(payload.get("source_backlog_id") or "")
            == source_backlog_id
            and str(payload.get("bypass_identity") or "")
            == expected_identity
        ):
            continue
        matched_bindings.append(
            {
                "diagnostic_backlog_id": diagnostic_backlog_id,
                "source_backlog_id": source_backlog_id,
                "source_contract_execution_id": contract_execution_id,
                "stage_id": str(line.get("stage_id") or ""),
                "line_id": str(line.get("line_id") or ""),
                "completed_line_index": line_index,
                "bypass_identity": expected_identity,
            }
        )
    if not matched_bindings:
        return {}

    return {
        "schema_version": (
            "contract_runtime.historical_audited_bypass_supersession.v1"
        ),
        "status": "WAIVED",
        "row_status": "WAIVED",
        "source_row_status": "WAIVED",
        "readiness_state": "completed_with_exception",
        "disposition": "completed_with_exception",
        "terminal": True,
        "scheduler_eligible": False,
        "schedulable": False,
        "current_eligible": False,
        "close_eligible": False,
        "closeable": False,
        "resume_eligible": False,
        "resumable": False,
        "source_backlog_mutated": False,
        "no_pass_claim": True,
        "terminal_basis": "historical_operator_supersession",
        "historical_operator_supersession": {
            "schema_version": (
                "contract_runtime.historical_operator_supersession.v1"
            ),
            "authority_ref": _HISTORICAL_BYPASS_OPERATOR_SUPERSESSION_REF,
            "operator_authorized": True,
            "immutable_source_evidence": True,
            "source_evidence_mutated": False,
            "current_generation_barrier_satisfied": False,
            "authoritative_pass_synthesized": False,
            "qa_pass_claimed": False,
            "merge_pass_claimed": False,
            "reconcile_pass_claimed": False,
            "matched_bindings": matched_bindings,
        },
        "repair_requires_separate_backlog_row": True,
        "fresh_generation_requires_accepted_repair_merge_and_current_head_reconcile": True,
    }


def _audited_bypass_recovery_fallback(
    record: Mapping[str, Any],
    terminal_disposition: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one advisory recovery envelope from a proven terminal bypass."""

    if not (
        str(terminal_disposition.get("schema_version") or "")
        == "contract_runtime.audited_bypass_terminal.v1"
        and terminal_disposition.get("terminal") is True
        and terminal_disposition.get("no_pass_claim") is True
        and str(terminal_disposition.get("status") or "").upper() == "WAIVED"
        and str(terminal_disposition.get("readiness_state") or "")
        == "completed_with_exception"
        and terminal_disposition.get("scheduler_eligible") is False
        and terminal_disposition.get("resume_eligible") is False
    ):
        return {}
    barrier = (
        terminal_disposition.get("terminal_barrier")
        if isinstance(terminal_disposition.get("terminal_barrier"), Mapping)
        else {}
    )
    try:
        bypass_index = int(barrier.get("bypass_line_index"))
        qa_index = int(barrier.get("qa_line_index"))
        merge_index = int(barrier.get("merge_line_index"))
        reconcile_index = int(barrier.get("reconcile_line_index"))
    except (TypeError, ValueError):
        return {}
    reconcile_source_ref = str(
        barrier.get("reconcile_source_ref") or ""
    ).strip()
    if not (
        bypass_index >= 0
        and qa_index >= 0
        and merge_index > qa_index
        and reconcile_index > merge_index
        and reconcile_source_ref.startswith("timeline:")
    ):
        return {}

    diagnostic_backlog_id = str(
        terminal_disposition.get("diagnostic_backlog_id") or ""
    ).strip()
    repair_target_known = bool(diagnostic_backlog_id)
    return {
        "schema_version": "contract_runtime.bypass_recovery_fallback.v1",
        "status": "ready" if repair_target_known else "unknown",
        "mode": "upstream_audited_bypass_recovery",
        "navigation_model": "single_unified_fallback",
        "per_gate_checklist_mapping": False,
        "authority": "advisory_navigation",
        "advisory_only": True,
        "authorizes_write": False,
        "authorizes_pass": False,
        "satisfies_gate": False,
        "mutates_runtime_state": False,
        "detected_from_durable_server_evidence": True,
        "source_backlog_id": str(record.get("backlog_id") or ""),
        "source_contract_execution_id": str(
            record.get("contract_execution_id") or ""
        ),
        "source_contract_id": str(record.get("contract_id") or ""),
        "source_terminal_disposition": "WAIVED/completed_with_exception",
        "source_scheduler_eligible": False,
        "source_resume_eligible": False,
        "source_status_may_become_fixed": False,
        "diagnostic_backlog_id": diagnostic_backlog_id,
        "repair_target": {
            "status": "known" if repair_target_known else "unknown",
            "backlog_id": diagnostic_backlog_id,
            "source": (
                "canonical_contract_line_bypass_payload"
                if repair_target_known
                else "durable_server_evidence_insufficient"
            ),
            "observer_confirmation_required": not repair_target_known,
        },
        "observer_confirmation_required": not repair_target_known,
        "generation_disposition": "discarded",
        "current_generation_must_not_resume": True,
        "downstream_missing_evidence_repair_forbidden": True,
        "demo_validation_bypass_allowed": False,
        "zero_bypass_happy_path_unchanged": True,
        "required_sequence": list(
            _BYPASS_RECOVERY_FALLBACK_REQUIRED_SEQUENCE
        ),
        "forbidden_actions": list(
            _BYPASS_RECOVERY_FALLBACK_FORBIDDEN_ACTIONS
        ),
        "durable_evidence": {
            "source": "contract_runtime_executions.completed_lines",
            "bypass_identity": str(
                terminal_disposition.get("bypass_identity") or ""
            ),
            "bypassed_stage_id": str(
                terminal_disposition.get("stage_id") or ""
            ),
            "bypassed_line_id": str(
                terminal_disposition.get("line_id") or ""
            ),
            "qa_line_index": qa_index,
            "merge_line_index": merge_index,
            "reconcile_line_index": reconcile_index,
            "current_head_full_reconcile_ref": reconcile_source_ref,
            "no_pass_claim": True,
        },
        "prompt": (
            "The upstream audited bypass source is terminal. Do not return to "
            "the historical source or repair downstream missing evidence. "
            "Continue only with an independently bounded root repair row, "
            "independent QA, ordered batch merge, current-HEAD full reconcile, "
            "then a fresh validation generation from scenario 1."
        ),
    }


def _canonical_current_full_reconcile_activation(
    reconcile_authority: Mapping[str, Any],
    *,
    expected_merge_source_ref: str = "",
    expected_merged_commit: str = "",
    expected_project_id: str = "",
    expected_backlog_id: str = "",
    expected_contract_execution_id: str = "",
) -> dict[str, Any]:
    """Return only server-proven current-HEAD full-reconcile authority."""

    candidate = (
        reconcile_authority.get(
            "terminal_current_full_reconcile_authority"
        )
        if isinstance(
            reconcile_authority.get(
                "terminal_current_full_reconcile_authority"
            ),
            Mapping,
        )
        else reconcile_authority
    )
    if not isinstance(candidate, Mapping):
        return {}
    canonical_head = str(
        candidate.get("canonical_head_commit")
        or candidate.get("current_canonical_commit_sha")
        or ""
    ).strip().lower()
    active_snapshot_commit = str(
        candidate.get("active_snapshot_commit") or ""
    ).strip().lower()
    reconciled_commit = str(
        candidate.get("reconciled_commit_sha") or ""
    ).strip().lower()
    provenance_target = str(
        candidate.get("reconcile_provenance_target_commit") or ""
    ).strip().lower()
    merge_source_ref = str(
        candidate.get("merge_source_ref") or ""
    ).strip()
    merged_commit = str(
        candidate.get("merged_commit_sha") or ""
    ).strip().lower()
    reconcile_source_ref = str(
        candidate.get("reconcile_source_ref") or ""
    ).strip()
    required_true_fields = (
        "db_verified",
        "live_verified",
        "canonical_head_verified",
        "active_snapshot_verified",
        "active_snapshot_matches_canonical_head",
        "graph_reconciled",
        "provenance_verified",
        "provenance_scope_verified",
        "durable_order_verified",
        "reconcile_snapshot_verified",
        "contract_execution_scope_verified",
        "task_scope_verified",
        "runtime_context_scope_verified",
        "parent_task_scope_verified",
        "merge_queue_scope_verified",
    )
    if not (
        str(candidate.get("schema_version") or "")
        == "graph_snapshot_store.current_full_reconcile_state.v1"
        and candidate.get("server_derived") is True
        and str(candidate.get("source") or "")
        == "graph_snapshot_store.current_full_reconcile_state"
        and all(candidate.get(field) is True for field in required_true_fields)
        and candidate.get("current_full_reconcile") is True
        and str(candidate.get("strategy") or "")
        == "current_full_reconcile"
        and str(candidate.get("active_snapshot_status") or "") == "active"
        and canonical_head
        and active_snapshot_commit == canonical_head
        and reconciled_commit == canonical_head
        and provenance_target == canonical_head
        and reconcile_source_ref.startswith("timeline:")
        and (
            not expected_merge_source_ref
            or merge_source_ref == expected_merge_source_ref
        )
        and (
            not expected_merged_commit
            or merged_commit == expected_merged_commit
        )
        and (
            not expected_project_id
            or str(candidate.get("project_id") or "") == expected_project_id
        )
        and (
            not expected_backlog_id
            or str(candidate.get("backlog_id") or "") == expected_backlog_id
        )
        and (
            not expected_contract_execution_id
            or str(candidate.get("contract_execution_id") or "")
            == expected_contract_execution_id
        )
        and str(candidate.get("authority_hash") or "")
        == stable_sha256(
            {
                key: value
                for key, value in candidate.items()
                if key != "authority_hash"
            }
        )
    ):
        return {}
    return dict(candidate)


def _audited_bypass_terminal_disposition(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    completed_lines = record.get("completed_lines")
    if not isinstance(completed_lines, Sequence):
        return {}
    bypass_index = -1
    bypass_line: Mapping[str, Any] = {}
    canonical_bypasses: list[
        tuple[int, Mapping[str, Any], Mapping[str, Any]]
    ] = []
    qa_barrier_index = -1
    qa_after_index = -1
    qa_barrier_is_audit_only_bypass = False
    qa_candidate_commit = ""
    merge_index = -1
    merge_after_index = -1
    merge_authority: Mapping[str, Any] = {}
    reconcile_index = -1
    reconcile_after_index = -1
    reconcile_authority: Mapping[str, Any] = {}
    live_forward_path_valid = True
    qa_stage_closed = False
    merge_stage_closed = False
    reconcile_stage_closed = False

    def qa_barrier_ready() -> bool:
        return qa_barrier_index > qa_after_index

    def merge_barrier_ready() -> bool:
        return bool(
            qa_barrier_ready()
            and merge_index > qa_barrier_index
            and merge_index > merge_after_index
        )

    def reconcile_barrier_ready() -> bool:
        return bool(
            merge_barrier_ready()
            and reconcile_index > merge_index
            and reconcile_index > reconcile_after_index
        )

    for index, line in enumerate(completed_lines):
        if not isinstance(line, Mapping):
            continue
        line_payload = (
            line.get("payload")
            if isinstance(line.get("payload"), Mapping)
            else {}
        )
        bypass_payload = _canonical_audited_bypass_payload(line)
        if bypass_payload:
            bypass_index = index
            bypass_line = line
            canonical_bypasses.append((index, line, bypass_payload))
            bypass_line_id = str(line.get("line_id") or "")
            if bypass_line_id == "qa_independent_verification":
                qa_barrier_index = index
                qa_barrier_is_audit_only_bypass = True
                qa_candidate_commit = ""
                qa_stage_closed = True
                merge_after_index = max(merge_after_index, index)
                reconcile_after_index = max(reconcile_after_index, index)
            elif bypass_line_id == "observer_merge":
                if not qa_barrier_ready():
                    live_forward_path_valid = False
                qa_stage_closed = True
                merge_after_index = max(merge_after_index, index)
                reconcile_after_index = max(reconcile_after_index, index)
            elif bypass_line_id == "observer_reconcile":
                if not merge_barrier_ready():
                    live_forward_path_valid = False
                qa_stage_closed = True
                merge_stage_closed = True
                reconcile_after_index = max(reconcile_after_index, index)
            elif bypass_line_id == "observer_close_ready":
                if not reconcile_barrier_ready():
                    live_forward_path_valid = False
                qa_stage_closed = True
                merge_stage_closed = True
                reconcile_stage_closed = True
            else:
                qa_after_index = max(qa_after_index, index)
                merge_after_index = max(merge_after_index, index)
                reconcile_after_index = max(reconcile_after_index, index)
            continue
        if (
            str(line.get("line_id") or "") == "qa_independent_verification"
            and str(line.get("actor_role") or "") == "qa"
            and str(line.get("evidence_kind") or "")
            == "independent_verification"
            and str(line.get("status") or "").lower()
            in {"accepted", "ok", "pass", "passed", "succeeded", "success"}
        ):
            if qa_stage_closed:
                live_forward_path_valid = False
                continue
            qa_barrier_index = index
            qa_barrier_is_audit_only_bypass = False
            qa_candidate_commit = str(
                line.get("commit_sha")
                or line_payload.get("candidate_commit_sha")
                or line_payload.get("commit_sha")
                or ""
            ).strip().lower()
            continue
        durable_merge = (
            line_payload.get("durable_merge_authority")
            if isinstance(line_payload.get("durable_merge_authority"), Mapping)
            else {}
        )
        audit_only_qa = (
            durable_merge.get("qa_audit_only_no_pass_authority")
            if isinstance(
                durable_merge.get("qa_audit_only_no_pass_authority"), Mapping
            )
            else {}
        )
        try:
            authority_qa_index = int(
                durable_merge.get("qa_completed_line_index")
            )
        except (TypeError, ValueError):
            authority_qa_index = -1
        try:
            audit_only_bypass_index = int(
                audit_only_qa.get("bypass_completed_line_index")
            )
        except (TypeError, ValueError):
            audit_only_bypass_index = -1
        try:
            audit_only_qa_index = int(
                audit_only_qa.get(
                    "qa_independent_verification_completed_line_index"
                )
            )
        except (TypeError, ValueError):
            audit_only_qa_index = -1
        merge_candidate_commit = str(
            durable_merge.get("branch_head") or ""
        ).strip().lower()
        ordinary_qa_bound = bool(
            qa_barrier_ready()
            and not qa_barrier_is_audit_only_bypass
            and durable_merge.get("qa_contract_runtime_verified") is True
            and str(durable_merge.get("qa_acceptance_ref") or "")
            and (
                authority_qa_index < 0
                or authority_qa_index == qa_barrier_index
            )
            and (
                not qa_candidate_commit
                or not merge_candidate_commit
                or qa_candidate_commit == merge_candidate_commit
            )
        )
        graph_context_bypass_round_bound = bool(
            not qa_barrier_is_audit_only_bypass
            and str(audit_only_qa.get("source_shape") or "")
            == "qa_graph_context_bypass_then_independent_verification"
            and str(audit_only_qa.get("bypass_line_id") or "")
            == "qa_graph_context"
            and audit_only_bypass_index == bypass_index
            and audit_only_qa_index == qa_barrier_index
            and (
                authority_qa_index < 0
                or authority_qa_index == qa_barrier_index
            )
            and (
                not qa_candidate_commit
                or str(audit_only_qa.get("candidate_commit_sha") or "")
                .strip()
                .lower()
                == qa_candidate_commit
            )
            and (
                not merge_candidate_commit
                or str(audit_only_qa.get("candidate_commit_sha") or "")
                .strip()
                .lower()
                == merge_candidate_commit
            )
        )
        audited_no_pass_bound = bool(
            qa_barrier_ready()
            and (
                qa_barrier_is_audit_only_bypass
                or graph_context_bypass_round_bound
            )
            and str(audit_only_qa.get("schema_version") or "")
            == "contract_runtime.audit_only_no_pass_bypass_round_authority.v1"
            and audit_only_qa.get("server_derived") is True
            and audit_only_qa.get("db_verified") is True
            and audit_only_qa.get("no_pass_claim") is True
            and audit_only_qa.get("authoritative_pass_synthesized") is False
            and str(audit_only_qa.get("authority_hash") or "")
            == str(durable_merge.get("qa_acceptance_ref") or "")
            and str(audit_only_qa.get("authority_hash") or "")
            == stable_sha256(
                {
                    key: value
                    for key, value in audit_only_qa.items()
                    if key != "authority_hash"
                }
            )
        )
        if (
            qa_barrier_ready()
            and index > qa_barrier_index
            and index > merge_after_index
            and str(line.get("line_id") or "") == "observer_merge"
            and str(line.get("actor_role") or "") == "observer"
            and str(line.get("evidence_kind") or "") == "merge"
            and _line_status_allows_contract_completion(
                line,
                source_record=record,
                source_line_index=index,
            )
            and str(durable_merge.get("schema_version") or "")
            == "contract_runtime.observer_merge_durable_authority.v1"
            and durable_merge.get("server_derived") is True
            and durable_merge.get("db_verified") is True
            and durable_merge.get("merge_gate_passed") is True
            and str(durable_merge.get("merge_event_ref") or "").startswith(
                "timeline:"
            )
            and (ordinary_qa_bound or audited_no_pass_bound)
        ):
            if merge_stage_closed:
                live_forward_path_valid = False
                continue
            merge_index = index
            merge_authority = durable_merge
            continue
        if not (
            merge_barrier_ready()
            and index > merge_index
            and index > reconcile_after_index
            and str(line.get("line_id") or "") == "observer_reconcile"
            and str(line.get("actor_role") or "") == "observer"
            and str(line.get("evidence_kind") or "") == "reconcile"
            and _line_status_allows_contract_completion(
                line,
                source_record=record,
                source_line_index=index,
            )
        ):
            continue
        if reconcile_stage_closed:
            live_forward_path_valid = False
            continue
        reconcile_payload = (
            line.get("payload")
            if isinstance(line.get("payload"), Mapping)
            else {}
        )
        candidate_reconcile_authority = (
            reconcile_payload.get("reconcile_authority")
            if isinstance(reconcile_payload.get("reconcile_authority"), Mapping)
            else {}
        )
        authority_schema = str(
            candidate_reconcile_authority.get("schema_version") or ""
        )
        if authority_schema == (
            "contract_runtime.observer_reconcile_record_authority.v1"
        ) and not (
            candidate_reconcile_authority.get("server_derived") is True
            and candidate_reconcile_authority.get("record_verified") is True
            and candidate_reconcile_authority.get("merge_projection_verified")
            is True
            and candidate_reconcile_authority.get(
                "dispatch_lineage_verified"
            )
            is True
            and candidate_reconcile_authority.get("reconcile_event_recorded")
            is True
            and str(candidate_reconcile_authority.get("merge_source_ref") or "")
            == str(merge_authority.get("merge_event_ref") or "")
            and str(candidate_reconcile_authority.get("merged_commit_sha") or "")
            == str(merge_authority.get("merge_commit") or "")
            and str(
                candidate_reconcile_authority.get("reconcile_source_ref") or ""
            ).startswith("timeline:")
            and str(candidate_reconcile_authority.get("authority_hash") or "")
            == stable_sha256(
                {
                    key: value
                    for key, value in candidate_reconcile_authority.items()
                    if key != "authority_hash"
                }
            )
        ):
            continue
        current_full_authority = _canonical_current_full_reconcile_activation(
            candidate_reconcile_authority,
            expected_merge_source_ref=str(
                merge_authority.get("merge_event_ref") or ""
            ),
            expected_merged_commit=str(
                merge_authority.get("merge_commit") or ""
            ),
            expected_project_id=str(record.get("project_id") or ""),
            expected_backlog_id=str(record.get("backlog_id") or ""),
            expected_contract_execution_id=str(
                record.get("contract_execution_id") or ""
            ),
        )
        if not current_full_authority:
            continue
        reconcile_index = index
        reconcile_authority = current_full_authority

    if (
        bypass_index >= 0
        and live_forward_path_valid
        and reconcile_barrier_ready()
    ):
        bypass_payload = _canonical_audited_bypass_payload(bypass_line)
        terminal = {
            "schema_version": "contract_runtime.audited_bypass_terminal.v1",
            "status": "WAIVED",
            "row_status": "WAIVED",
            "source_row_status": "WAIVED",
            "readiness_state": "completed_with_exception",
            "disposition": "completed_with_exception",
            "terminal": True,
            "scheduler_eligible": False,
            "schedulable": False,
            "current_eligible": False,
            "close_eligible": False,
            "closeable": False,
            "resume_eligible": False,
            "resumable": False,
            "source_backlog_mutated": False,
            "diagnostic_backlog_id": str(
                bypass_payload.get("diagnostic_backlog_id") or ""
            ),
            "bypass_identity": str(bypass_payload.get("bypass_identity") or ""),
            "line_id": str(bypass_line.get("line_id") or ""),
            "stage_id": str(bypass_line.get("stage_id") or ""),
            "no_pass_claim": True,
            "terminal_barrier": {
                "bypass_line_index": bypass_index,
                "qa_line_index": qa_barrier_index,
                "merge_line_index": merge_index,
                "reconcile_line_index": reconcile_index,
                "reconcile_source_ref": str(
                    reconcile_authority.get("reconcile_source_ref") or ""
                ),
                "active_snapshot_id": str(
                    reconcile_authority.get("active_snapshot_id") or ""
                ),
                "active_snapshot_commit": str(
                    reconcile_authority.get("active_snapshot_commit") or ""
                ),
                "canonical_head_commit": str(
                    reconcile_authority.get("canonical_head_commit")
                    or reconcile_authority.get(
                        "current_canonical_commit_sha"
                    )
                    or ""
                ),
                "reconcile_provenance_target_commit": str(
                    reconcile_authority.get(
                        "reconcile_provenance_target_commit"
                    )
                    or ""
                ),
                "ordering_policy": "stage_aware_forward_only",
            },
            "repair_requires_separate_backlog_row": True,
            "fresh_generation_requires_accepted_repair_merge_and_current_head_reconcile": True,
        }
        recovery_fallback = _audited_bypass_recovery_fallback(record, terminal)
        if recovery_fallback:
            terminal["bypass_recovery_fallback"] = recovery_fallback
        return terminal
    return _historical_audited_bypass_supersession_disposition(
        record,
        canonical_bypasses,
    )


def _record_is_complete(record: Mapping[str, Any]) -> bool:
    guide = (
        record.get("runtime_guide")
        if isinstance(record.get("runtime_guide"), Mapping)
        else {}
    )
    return guide.get("next_legal_action") is None


def _pinned_terminal_no_pass_disposition(
    definition: Mapping[str, Any],
    lines: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive terminal no-PASS only from the pinned Contract policy and Fact."""

    system_layer = (
        definition.get("system_layer")
        if isinstance(definition.get("system_layer"), Mapping)
        else {}
    )
    policy = (
        system_layer.get("terminal_outcome_policy")
        if isinstance(system_layer.get("terminal_outcome_policy"), Mapping)
        else {}
    )
    if not policy:
        return {}
    failure_statuses = {
        "blocked",
        "fail",
        "failed",
        "failure",
        "rejected",
    }
    selected: tuple[int, Mapping[str, Any], str] | None = None
    for index, line in enumerate(lines):
        if not isinstance(line, Mapping):
            continue
        line_id = str(line.get("line_id") or "").strip()
        status = str(line.get("status") or "").strip().lower()
        if status not in failure_statuses:
            continue
        policy_field = (
            "qa_failure"
            if line_id == "qa_independent_verification"
            else "authoritative_close_failure"
            if line_id == "observer_close_ready"
            else ""
        )
        if not policy_field or not str(policy.get(policy_field) or "").startswith(
            "terminal_no_pass"
        ):
            continue
        selected = (index, line, policy_field)
    if selected is None:
        return {}
    source_index, source_line, policy_field = selected
    disposition = {
        "schema_version": "contract_runtime.pinned_terminal_no_pass_disposition.v1",
        "status": "FAILED",
        "readiness_state": "terminal_no_pass",
        "disposition": "terminal_no_pass",
        "terminal": True,
        "scheduler_eligible": False,
        "schedulable": False,
        "current_eligible": False,
        "close_eligible": False,
        "closeable": False,
        "resume_eligible": False,
        "resumable": False,
        "retry_eligible": False,
        "write_eligible": False,
        "no_pass_claim": True,
        "authoritative_pass_synthesized": False,
        "history_rewrite_allowed": False,
        "repair_requires_separate_backlog_row": True,
        "source_completed_line_index": source_index,
        "source_stage_id": str(source_line.get("stage_id") or ""),
        "source_line_id": str(source_line.get("line_id") or ""),
        "source_status": str(source_line.get("status") or ""),
        "source_line_hash": stable_sha256(dict(source_line)),
        "policy_field": policy_field,
        "policy_value": str(policy.get(policy_field) or ""),
        "source_of_authority": "pinned_contract_terminal_outcome_policy",
    }
    disposition["disposition_hash"] = stable_sha256(disposition)
    return disposition


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
    after_index = _last_failed_qa_line_index(lines, source_record=record)
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
        if not _line_status_allows_direct_fix_qa(
            line,
            source_record=record,
            source_line_index=_source_record_completed_line_index(line, record),
        ):
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


def _line_status_allows_direct_fix_qa(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None = None,
    source_line_index: int = -1,
) -> bool:
    return _line_status_allows_contract_completion(
        line,
        source_record=source_record,
        source_line_index=source_line_index,
    )


_CONTRACT_COMPLETION_BLOCKING_STATUSES = frozenset(
    {"fail", "failed", "failure", "rejected", "blocked"}
)
_QA_COMPLETION_PASSING_STATUSES = frozenset(
    {"accepted", "ok", "pass", "passed", "succeeded", "success"}
)
_QA_COMPLETION_STATUS_GATE_SCHEMA_VERSION = (
    "contract_runtime.qa_completion_status_gate.v1"
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
    source_record: Mapping[str, Any] | None = None,
    failed_qa_rejoin_contexts: set[tuple[str, str]] | None = None,
    failed_qa_rejoin_markers: Sequence[Mapping[str, Any]] | None = None,
) -> list[Mapping[str, Any]]:
    last_failed_qa_index = _last_failed_qa_line_index(
        lines,
        source_record=source_record,
    )
    post_failed_retry_contexts = _post_failed_qa_retry_context_keys(
        lines,
        failed_qa_index=last_failed_qa_index,
        source_record=source_record,
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
            line,
            source_record=source_record,
            source_line_index=_source_record_completed_line_index(
                line,
                source_record,
            ),
        ):
            continue
        satisfying.append(line)
    return satisfying


def _source_record_completed_line_index(
    line: Mapping[str, Any],
    source_record: Mapping[str, Any] | None,
) -> int:
    """Map a projected line to its unique persisted source member."""

    if not isinstance(source_record, Mapping):
        return -1
    stored_lines = source_record.get("completed_lines")
    if not isinstance(stored_lines, list):
        return -1
    persisted_line_indexes = [
        index
        for index, stored_line in enumerate(stored_lines)
        if stored_line is line or stored_line == line
    ]
    if len(persisted_line_indexes) != 1:
        return -1
    return persisted_line_indexes[0]


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


def _active_failed_qa_line_index(
    lines: Sequence[Mapping[str, Any]],
    *,
    source_record: Mapping[str, Any] | None = None,
) -> int:
    failed_index = _last_failed_qa_line_index(
        lines,
        source_record=source_record,
    )
    if failed_index < 0:
        return -1
    for index, line in enumerate(lines):
        if index <= failed_index or not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() != "qa_independent_verification":
            continue
        if not _line_shape_allows_contract_completion(line):
            continue
        if _qa_line_supersedes_active_failed_qa(
            line,
            source_record=source_record,
            source_line_index=_source_record_completed_line_index(
                line,
                source_record,
            ),
        ):
            return -1
    return failed_index


def _active_failed_qa_line(
    lines: Sequence[Mapping[str, Any]],
    *,
    source_record: Mapping[str, Any] | None = None,
) -> tuple[int, Mapping[str, Any]]:
    """Return the QA line selected by the canonical completion state machine.

    Runtime Context recovery must not maintain a second, looser definition of
    failed independent QA.  In particular, an accepted no-PASS line whose
    redundant result counts disagree with its immutable baseline ledger is an
    active failed-QA boundary even when candidate-new failures are zero.
    """

    failed_index = _active_failed_qa_line_index(
        lines,
        source_record=source_record,
    )
    if failed_index < 0 or failed_index >= len(lines):
        return -1, {}
    failed_line = lines[failed_index]
    if not isinstance(failed_line, Mapping):
        return -1, {}
    return failed_index, failed_line


def _attach_failed_qa_rework_guidance(
    guide: dict[str, Any],
    *,
    line_items: Sequence[Mapping[str, Any]],
    source_record: Mapping[str, Any] | None = None,
) -> None:
    failed_index = _active_failed_qa_line_index(
        line_items,
        source_record=source_record,
    )
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
        "expected_diff_base_source": "runtime_context.base_commit",
        "changed_files_semantics": "cumulative_runtime_diff",
        "required_submission": "changed_files=cumulative_runtime_diff",
        "delta_rework_changed_files_allowed": False,
        "reason": (
            "Independent QA recorded a failing verdict; merge/materialize stay "
            "blocked until a worker revision and a later passing independent QA line."
        ),
    }
    next_action.setdefault("semantic_next_action", blocker["semantic_next_action"])
    next_action.setdefault("blocked_by_failed_qa", True)
    next_action.setdefault("failed_qa_blocker", blocker)
    guide.setdefault("failed_qa_rework", blocker)


def _last_failed_qa_line_index(
    lines: Sequence[Mapping[str, Any]],
    *,
    source_record: Mapping[str, Any] | None = None,
) -> int:
    failed_index = -1
    for index, line in enumerate(lines):
        if not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() != "qa_independent_verification":
            continue
        if not _qa_line_supersedes_active_failed_qa(
            line,
            source_record=source_record,
            source_line_index=_source_record_completed_line_index(
                line,
                source_record,
            ),
        ):
            failed_index = index
    return failed_index


def _qa_line_supersedes_active_failed_qa(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None = None,
    source_line_index: int = -1,
) -> bool:
    """Return whether a later QA line clears the active repair boundary.

    Completion remains strictly server-normalized.  Failed-QA recovery also
    has to understand one historical/synthetic projection: callers copied a
    failed QA line, replaced its top-level and payload statuses with ``passed``,
    but retained the earlier normalization gate that says no top-level status
    was present.  That explicit later pass supersedes the older repair
    authority without making the copied line close-satisfying.
    """

    if _line_status_allows_contract_completion(
        line,
        source_record=source_record,
        source_line_index=source_line_index,
    ):
        return True
    if str(line.get("line_id") or "").strip() != "qa_independent_verification":
        return False
    if str(line.get("actor_role") or "").strip().lower() != "qa":
        return False
    if bool(line.get("observer_impersonation")):
        return False
    payload = (
        line.get("payload")
        if isinstance(line.get("payload"), Mapping)
        else {}
    )
    if (
        str(line.get("status") or "").strip().lower()
        not in _QA_COMPLETION_PASSING_STATUSES
        or str(payload.get("status") or "").strip().lower()
        not in _QA_COMPLETION_PASSING_STATUSES
    ):
        return False
    provenance = (
        line.get("qa_evidence_provenance")
        if isinstance(line.get("qa_evidence_provenance"), Mapping)
        else {}
    )
    status_gate = (
        provenance.get("completion_status_gate")
        if isinstance(provenance.get("completion_status_gate"), Mapping)
        else {}
    )
    if not (
        str(status_gate.get("schema_version") or "")
        == _QA_COMPLETION_STATUS_GATE_SCHEMA_VERSION
        and status_gate.get("server_derived") is True
        and status_gate.get("top_level_status_present") is False
        and status_gate.get("top_level_status_passing") is False
        and not str(status_gate.get("normalized_status") or "").strip()
    ):
        return False
    if (
        _mapping_own_fields_contain_contract_completion_blocker(payload)
        or _contains_contract_completion_blocker(payload)
        or _qa_independent_verification_summary_reports_failure(payload)
    ):
        return False
    return True


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
        "qa_graph_context",
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
    source_record: Mapping[str, Any] | None = None,
) -> set[tuple[str, str]]:
    if failed_qa_index < 0:
        return set()
    contexts: set[tuple[str, str]] = set()
    for index, line in enumerate(
        lines[failed_qa_index + 1 :],
        start=failed_qa_index + 1,
    ):
        if not isinstance(line, Mapping):
            continue
        if str(line.get("line_id") or "").strip() not in _FAILED_QA_RETRY_PROOF_LINE_IDS:
            continue
        if not _line_status_allows_contract_completion(
            line,
            source_record=source_record,
            source_line_index=_source_record_completed_line_index(
                line,
                source_record,
            ),
        ):
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


def _contract_completion_satisfying_lines_for_view(
    definition: Mapping[str, Any],
    source_record: Mapping[str, Any],
    line_items: Sequence[Mapping[str, Any]],
    projection: Mapping[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    """Return the exact line set used by the authoritative execution state."""

    completion_input_lines = _compat_completion_lines_for_record(
        source_record,
        definition,
        line_items,
    )
    return _contract_completion_satisfying_lines(
        completion_input_lines,
        source_record=source_record,
        failed_qa_rejoin_contexts=(
            _failed_qa_rejoin_context_keys_from_projection(projection)
        ),
        failed_qa_rejoin_markers=(
            _failed_qa_rejoin_markers_from_projection(projection)
        ),
    )


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


def _worker_fence_containment(
    changed_files: Sequence[Any],
    owned_files: Sequence[Any],
    *,
    repository_root: str = "",
) -> dict[str, Any]:
    """Validate file changes against exact-file and recursive-directory roots.

    A trailing slash is the only directory-root marker. All other owned paths
    are exact files. Both sides are repository-relative canonical POSIX paths;
    aliases and escape-shaped paths fail closed instead of being normalized
    into authority.
    """

    def _normalized_path(value: Any, *, allow_directory: bool) -> tuple[str, bool]:
        raw = str(value or "").strip()
        if (
            not raw
            or "\x00" in raw
            or "\\" in raw
            or raw.startswith("/")
            or re.match(r"^[A-Za-z]:", raw)
        ):
            return "", False
        is_directory = raw.endswith("/")
        if is_directory and not allow_directory:
            return "", False
        candidate = raw[:-1] if is_directory else raw
        parts = candidate.split("/")
        if (
            not candidate
            or candidate in {".", ".."}
            or any(part in {"", ".", ".."} for part in parts)
        ):
            return "", False
        normalized = "/".join(parts)
        if normalized != candidate:
            return "", False
        return normalized, is_directory

    raw_changed = sorted(
        {
            str(value or "").strip()
            for value in changed_files
            if str(value or "").strip()
        }
    )
    raw_owned = sorted(
        {
            str(value or "").strip()
            for value in owned_files
            if str(value or "").strip()
        }
    )
    root_path: Path | None = None
    repository_root_valid = True
    if repository_root:
        candidate_root = Path(repository_root).expanduser()
        repository_root_valid = candidate_root.is_absolute()
        if repository_root_valid:
            root_path = candidate_root.resolve(strict=False)

    def _escapes_repository(normalized: str) -> bool:
        if root_path is None:
            return False
        resolved = (root_path / normalized).resolve(strict=False)
        try:
            resolved.relative_to(root_path)
        except ValueError:
            return True
        return False

    normalized_roots: list[tuple[str, bool]] = []
    invalid_owned: list[str] = []
    canonical_escape_roots: list[str] = []
    for raw in raw_owned:
        normalized, is_directory = _normalized_path(
            raw,
            allow_directory=True,
        )
        if not normalized:
            invalid_owned.append(raw)
            continue
        if _escapes_repository(normalized):
            invalid_owned.append(raw)
            canonical_escape_roots.append(raw)
            continue
        normalized_roots.append((normalized, is_directory))

    normalized_changed: list[str] = []
    invalid_changed: list[str] = []
    canonical_escape_files: list[str] = []
    for raw in raw_changed:
        normalized, is_directory = _normalized_path(
            raw,
            allow_directory=False,
        )
        if not normalized or is_directory:
            invalid_changed.append(raw)
            continue
        if _escapes_repository(normalized):
            invalid_changed.append(raw)
            canonical_escape_files.append(raw)
            continue
        normalized_changed.append(normalized)

    out_of_fence = [
        changed
        for changed in normalized_changed
        if not any(
            changed == root
            if not is_directory
            else changed.startswith(root + "/")
            for root, is_directory in normalized_roots
        )
    ]
    if invalid_owned or not repository_root_valid:
        out_of_fence = sorted(set(out_of_fence) | set(normalized_changed))
    out_of_fence = sorted(set(out_of_fence) | set(invalid_changed))
    return {
        "schema_version": "contract_runtime.worker_fence_containment.v1",
        "ok": bool(
            raw_changed
            and raw_owned
            and repository_root_valid
            and not invalid_owned
            and not invalid_changed
            and not out_of_fence
        ),
        "repository_root": str(root_path or ""),
        "repository_root_valid": repository_root_valid,
        "normalized_changed_files": sorted(set(normalized_changed)),
        "normalized_owned_files": sorted(
            {
                root + ("/" if is_directory else "")
                for root, is_directory in normalized_roots
            }
        ),
        "invalid_changed_files": sorted(set(invalid_changed)),
        "invalid_owned_files": sorted(set(invalid_owned)),
        "canonical_escape_files": sorted(set(canonical_escape_files)),
        "canonical_escape_roots": sorted(set(canonical_escape_roots)),
        "out_of_fence_files": out_of_fence,
    }


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
    lines = list(record.get("completed_lines") or [])
    for index, line in reversed(list(enumerate(lines))):
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
        if not _line_status_allows_contract_completion(
            line,
            source_record=record,
            source_line_index=index,
        ):
            continue
        return line
    return None


def _worker_implementation_lineage(
    record: Mapping[str, Any],
    implementation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stable public identity of a canonical implementation line."""

    lineage = {
        "schema_version": "contract_runtime.worker_implementation_lineage.v1",
        "source_of_authority": (
            "ContractRuntime.completed_lines.worker_implementation"
        ),
        "contract_execution_id": str(
            record.get("contract_execution_id") or ""
        ).strip(),
        "contract_id": str(record.get("contract_id") or "").strip(),
        "runtime_context_id": _worker_commit_text(
            implementation,
            "runtime_context_id",
        ),
        "task_id": _worker_commit_text(implementation, "task_id"),
        "stage_id": str(
            implementation.get("stage_id") or "worker_implementation"
        ).strip(),
        "line_id": "worker_implementation",
        "line_instance_id": str(
            implementation.get("line_instance_id") or ""
        ).strip(),
        "evidence_kind": str(
            implementation.get("evidence_kind") or "implementation"
        ).strip(),
        "worker_id": _worker_commit_text(implementation, "worker_id"),
        "worker_slot_id": _worker_commit_text(
            implementation,
            "worker_slot_id",
            "lane_id",
        ),
        "changed_files": sorted(
            set(_worker_commit_strings(implementation, "changed_files"))
        ),
        "graph_trace_ids": sorted(
            set(
                _worker_commit_strings(
                    implementation,
                    "graph_trace_ids",
                    "graph_query_trace_ids",
                    "verified_trace_ids",
                )
            )
        ),
    }
    lineage["implementation_lineage_ref"] = (
        "contract-runtime:worker-implementation:" + stable_sha256(lineage)
    )
    return lineage


_WORKER_IMPLEMENTATION_FINISH_PASS_STATUSES = frozenset(
    {"pass", "passed", "ok", "succeeded", "success", "clean"}
)
_WORKER_IMPLEMENTATION_FINISH_AMBIGUOUS_STATUSES = frozenset(
    {
        "accepted",
        "blocked",
        "error",
        "errored",
        "fail",
        "failed",
        "failure",
        "partial_sibling_blocked",
        "rejected",
    }
)
_WORKER_IMPLEMENTATION_NO_PASS_STATUS = "accepted_with_known_baseline_failure"
_WORKER_IMPLEMENTATION_UNRELATED_BLOCK_STATUS = (
    "passed_with_unrelated_system_block_recorded"
)
_WORKER_IMPLEMENTATION_NO_PASS_COUNT_FIELDS = (
    "candidate_new_failures",
    "full_failed",
    "inherited_failed",
    "baseline_failed",
    "focused_passed",
    "full_passed",
    "baseline_passed",
)
_WORKER_IMPLEMENTATION_HISTORICAL_BASE_SELECTORS = frozenset(
    {"rev8_postmerge_qa_graph_binding or pre_rev8_qa_graph_binding"}
)
_WORKER_IMPLEMENTATION_TEST_RESULTS_CORRECTION_FIELDS = frozenset(
    {
        "schema_version",
        "correction_id",
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "source_completed_line_index",
        "source_line_instance_id",
        "source_implementation_lineage_ref",
        "source_line_sha256",
        "source_execution_state_revision",
        "source_test_results_sha256",
        "corrected_test_results",
        "corrected_test_results_sha256",
        "source_authority",
        "source_authority_sha256",
        "correction_reason",
        "correction_reason_sha256",
        "ordered_evidence_refs",
        "ordered_evidence_refs_sha256",
        "ordered_tests",
        "ordered_tests_sha256",
        "append_only",
        "original_completed_line_immutable",
        "copy_safe",
        "raw_credentials_persisted",
        "created_at",
    }
)
_WORKER_IMPLEMENTATION_TEST_RESULTS_CORRECTION_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "server_derived",
        "worker_role",
        "worker_id",
        "worker_slot_id",
        "session_token_ref",
        "fence_token_hash",
        "session_authority_event_ref",
        "graph_trace_ids",
        "db_verified_graph_traces",
        "raw_session_token_persisted",
        "raw_fence_token_persisted",
        "raw_route_token_persisted",
    }
)


def worker_implementation_source_line_sha256(
    implementation: Mapping[str, Any],
) -> str:
    """Hash the immutable ContractRuntime source line without precedence."""

    return stable_sha256(deepcopy(dict(implementation)))


def worker_implementation_source_execution_state_revision(
    implementation: Mapping[str, Any],
) -> int:
    """Resolve the immutable revision recorded on the source line itself."""

    payload = (
        implementation.get("payload")
        if isinstance(implementation.get("payload"), Mapping)
        else {}
    )
    candidates = [
        candidate
        for candidate in (
            implementation.get("execution_state_revision"),
            payload.get("execution_state_revision"),
        )
        if candidate is not None
    ]
    if (
        not candidates
        or any(
            not isinstance(candidate, int)
            or isinstance(candidate, bool)
            or candidate <= 0
            for candidate in candidates
        )
        or len(set(candidates)) != 1
    ):
        return 0
    return int(candidates[0])


def worker_implementation_test_results_correction_id(
    correction: Mapping[str, Any],
) -> str:
    id_core = {
        key: correction[key]
        for key in sorted(
            _WORKER_IMPLEMENTATION_TEST_RESULTS_CORRECTION_FIELDS
            - {"correction_id", "created_at", "source_authority"}
        )
        if key in correction
    }
    return "witr-correction:" + stable_sha256(id_core)


def _worker_implementation_test_results_contains_raw_credential_field(
    value: Any,
) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower())
            if (
                normalized
                in {
                    "token",
                    "raw_token",
                    "session_token",
                    "route_token",
                    "fence_token",
                    "access_token",
                    "refresh_token",
                    "bearer_token",
                    "api_key",
                    "apikey",
                    "password",
                    "secret",
                    "secret_value",
                    "credential",
                    "credentials",
                    "credential_value",
                    "authorization",
                }
                or (
                    normalized.startswith("raw_")
                    and normalized.endswith("_token")
                )
            ):
                return True
            if _worker_implementation_test_results_contains_raw_credential_field(
                nested
            ):
                return True
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return any(
            _worker_implementation_test_results_contains_raw_credential_field(item)
            for item in value
        )
    return False


def worker_implementation_copy_safe_test_command(value: Any) -> bool:
    command = value if isinstance(value, str) else ""
    stripped = command.strip()
    placeholder_vocabulary = {
        "placeholder",
        "replace-me",
        "replace_me",
        "tbd",
        "todo",
    }
    return bool(
        1 <= len(stripped) <= 512
        and command == stripped
        and not any(ord(character) < 32 for character in command)
        and stripped.lower() not in placeholder_vocabulary
        and not re.search(r"<[^<>\r\n]{1,128}>", command)
        and not re.search(r"\{\{[^{}\r\n]{1,128}\}\}", command)
        and not re.search(
            r"(?i)(?:\$\{|__)(?:todo|tbd|placeholder|replace[_-]?me)(?:\}|__)",
            command,
        )
        and not re.search(
            r"(?i)(?:token|password|secret|api[_-]?key|authorization)\s*(?:=|:)",
            command,
        )
        and not re.search(
            r"(?i)(?:^|\s)--(?:session-|route-|fence-)?"
            r"(?:token|password|secret|api-key)(?:\s|=)",
            command,
        )
        and not re.search(r"(?i)\bbearer\s+[a-z0-9._~-]+", command)
    )


def _worker_implementation_canonical_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not (20 <= len(value) <= 32):
        return False
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00",
        value,
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return bool(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0)


def worker_implementation_test_results_correction_validation(
    record: Mapping[str, Any],
    implementation: Mapping[str, Any],
    correction: Any,
) -> dict[str, Any]:
    """Validate one independent correction ledger row against its source."""

    schema_version = (
        "contract_runtime.worker_implementation_test_results_correction_validation.v1"
    )

    def _reject(reason: str) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "accepted": False,
            "reason": reason,
            "canonical_test_results": {},
        }

    if not isinstance(correction, Mapping):
        return _reject("correction_must_be_mapping")
    candidate = deepcopy(dict(correction))
    if set(candidate) != _WORKER_IMPLEMENTATION_TEST_RESULTS_CORRECTION_FIELDS:
        return _reject("correction_closed_schema_mismatch")
    if (
        str(candidate.get("schema_version") or "").strip()
        != "contract_runtime.worker_implementation_test_results_correction.v1"
        or candidate.get("append_only") is not True
        or candidate.get("original_completed_line_immutable") is not True
        or candidate.get("copy_safe") is not True
        or candidate.get("raw_credentials_persisted") is not False
        or not _worker_implementation_canonical_utc_timestamp(
            candidate.get("created_at")
        )
    ):
        return _reject("correction_contract_flags_invalid")
    source_authority = candidate.get("source_authority")
    if (
        not isinstance(source_authority, Mapping)
        or set(source_authority)
        != _WORKER_IMPLEMENTATION_TEST_RESULTS_CORRECTION_AUTHORITY_FIELDS
        or str(source_authority.get("schema_version") or "").strip()
        != "runtime_context.worker_implementation_test_results_correction_authority.v1"
        or str(source_authority.get("source") or "").strip()
        != "authenticated_runtime_context_implementation_evidence"
        or source_authority.get("server_derived") is not True
        or str(source_authority.get("worker_role") or "").strip() != "mf_sub"
        or source_authority.get("db_verified_graph_traces") is not True
        or not str(
            source_authority.get("session_authority_event_ref") or ""
        ).startswith("timeline:")
        or not list(source_authority.get("graph_trace_ids") or [])
        or source_authority.get("raw_session_token_persisted") is not False
        or source_authority.get("raw_fence_token_persisted") is not False
        or source_authority.get("raw_route_token_persisted") is not False
    ):
        return _reject("correction_source_authority_invalid")
    if stable_sha256(source_authority) != str(
        candidate.get("source_authority_sha256") or ""
    ).strip():
        return _reject("correction_source_authority_hash_mismatch")
    source_index = candidate.get("source_completed_line_index")
    source_revision = candidate.get("source_execution_state_revision")
    lines = list(record.get("completed_lines") or [])
    if (
        not isinstance(source_index, int)
        or isinstance(source_index, bool)
        or source_index < 0
        or source_index >= len(lines)
    ):
        return _reject("correction_source_index_or_revision_invalid")
    source_line = lines[source_index]
    expected_source_revision = (
        worker_implementation_source_execution_state_revision(source_line)
        if isinstance(source_line, Mapping)
        else 0
    )
    if (
        not isinstance(source_revision, int)
        or isinstance(source_revision, bool)
        or source_revision <= 0
        or source_revision != expected_source_revision
    ):
        return _reject("correction_source_index_or_revision_invalid")
    if (
        not isinstance(source_line, Mapping)
        or dict(source_line) != dict(implementation)
        or str(source_line.get("line_id") or "").strip()
        != "worker_implementation"
    ):
        return _reject("correction_source_line_mismatch")
    source_lineage = _worker_implementation_lineage(record, source_line)
    source_validation = worker_implementation_test_results_validation(
        source_line,
        evidence_envelope=True,
    )
    if source_validation.get("accepted") is True:
        return _reject("correction_source_already_finish_compatible")
    source_test_results = (
        source_line.get("payload", {}).get("test_results")
        if isinstance(source_line.get("payload"), Mapping)
        else source_line.get("test_results")
    )
    corrected_validation = worker_implementation_test_results_validation(
        candidate.get("corrected_test_results")
    )
    if corrected_validation.get("accepted") is not True:
        return _reject("correction_results_not_finish_compatible")
    correction_reason = candidate.get("correction_reason")
    ordered_evidence_refs = candidate.get("ordered_evidence_refs")
    ordered_tests = candidate.get("ordered_tests")
    if (
        not isinstance(correction_reason, str)
        or not (1 <= len(correction_reason.strip()) <= 512)
        or not isinstance(ordered_evidence_refs, list)
        or not ordered_evidence_refs
        or any(
            not isinstance(ref, str)
            or not ref.strip()
            or len(ref) > 256
            for ref in ordered_evidence_refs
        )
        or set(candidate.get("corrected_test_results") or {})
        != {"status", "passed", "commands"}
        or not isinstance(ordered_tests, list)
        or not ordered_tests
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"command", "status"}
            or not worker_implementation_copy_safe_test_command(
                item.get("command")
            )
            or not str(item.get("status") or "").strip()
            or len(str(item.get("status") or "").strip()) > 32
            or str(item.get("status") or "").strip().lower()
            not in _WORKER_IMPLEMENTATION_FINISH_PASS_STATUSES
            for item in ordered_tests
        )
        or candidate.get("corrected_test_results", {}).get("commands")
        != ordered_tests
        or stable_sha256(correction_reason.strip())
        != str(candidate.get("correction_reason_sha256") or "").strip()
        or stable_sha256(ordered_evidence_refs)
        != str(candidate.get("ordered_evidence_refs_sha256") or "").strip()
        or stable_sha256(ordered_tests)
        != str(candidate.get("ordered_tests_sha256") or "").strip()
    ):
        return _reject("correction_reason_or_ordered_evidence_invalid")
    expected_identity = {
        "project_id": str(record.get("project_id") or "").strip(),
        "backlog_id": str(record.get("backlog_id") or "").strip(),
        "contract_execution_id": str(
            record.get("contract_execution_id") or ""
        ).strip(),
        "runtime_context_id": _worker_commit_text(
            source_line, "runtime_context_id"
        ),
        "task_id": _worker_commit_text(source_line, "task_id"),
        "source_line_instance_id": str(
            source_line.get("line_instance_id") or ""
        ).strip(),
        "source_implementation_lineage_ref": str(
            source_lineage.get("implementation_lineage_ref") or ""
        ).strip(),
        "source_line_sha256": worker_implementation_source_line_sha256(
            source_line
        ),
        "source_test_results_sha256": stable_sha256(source_test_results),
        "corrected_test_results_sha256": stable_sha256(
            corrected_validation.get("canonical_test_results") or {}
        ),
    }
    if any(
        str(candidate.get(field) or "").strip() != expected
        for field, expected in expected_identity.items()
    ):
        return _reject("correction_identity_mismatch")
    if str(source_authority.get("worker_id") or "").strip() != _worker_commit_text(
        source_line, "worker_id"
    ) or str(source_authority.get("worker_slot_id") or "").strip() != (
        _worker_commit_text(source_line, "worker_slot_id", "lane_id")
    ):
        return _reject("correction_worker_authority_mismatch")
    expected_correction_id = worker_implementation_test_results_correction_id(
        candidate
    )
    if str(candidate.get("correction_id") or "").strip() != expected_correction_id:
        return _reject("correction_id_mismatch")
    return {
        "schema_version": schema_version,
        "accepted": True,
        "reason": "accepted",
        "canonical_test_results": dict(
            corrected_validation.get("canonical_test_results") or {}
        ),
        "correction_id": expected_correction_id,
    }


def _worker_implementation_legacy_results_finish_compatible(value: Any) -> bool:
    """Validate the one bounded candidate/base result accepted by the facade."""

    if not isinstance(value, Mapping) or not value:
        return False
    if (
        str(value.get("schema_version") or "").strip()
        != "runtime_context.worker_test_results.v1"
        or str(value.get("status") or "").strip().lower()
        in _WORKER_IMPLEMENTATION_FINISH_AMBIGUOUS_STATUSES
        or any(
            value.get(field) is True
            for field in (
                "passed",
                "qa_claim",
                "release_claim",
                "overall_release_pass",
                "overall_release_pass_claimed",
                "old_world_reuse",
            )
        )
    ):
        return False
    focused = value.get("focused_candidate")
    expanded = value.get("expanded_candidate")
    immutable_base = value.get("immutable_base")
    if not all(
        isinstance(item, Mapping)
        for item in (focused, expanded, immutable_base)
    ):
        return False
    if (
        str(immutable_base.get("selector") or "").strip()
        not in _WORKER_IMPLEMENTATION_HISTORICAL_BASE_SELECTORS
    ):
        return False

    def _count(container: Mapping[str, Any], field: str) -> int | None:
        candidate = container.get(field)
        if (
            not isinstance(candidate, int)
            or isinstance(candidate, bool)
            or candidate < 0
        ):
            return None
        return candidate

    candidate_new_failures = _count(value, "candidate_new_failures")
    focused_failed = _count(focused, "failed")
    focused_passed = _count(focused, "passed")
    expanded_failed = _count(expanded, "failed")
    expanded_passed = _count(expanded, "passed")
    base_failed = _count(immutable_base, "failed")
    base_passed = _count(immutable_base, "passed")
    counts = (
        candidate_new_failures,
        focused_failed,
        focused_passed,
        expanded_failed,
        expanded_passed,
        base_failed,
        base_passed,
    )
    if any(count is None for count in counts):
        return False
    if (
        candidate_new_failures != 0
        or focused_failed != 0
        or expanded_failed != 0
        or focused_passed <= 0
        or expanded_passed <= 0
        or base_failed <= 0
        or expanded_passed < base_passed
    ):
        return False
    if (
        str(focused.get("status") or "").strip().lower()
        not in _WORKER_IMPLEMENTATION_FINISH_PASS_STATUSES
        or str(expanded.get("status") or "").strip().lower()
        not in _WORKER_IMPLEMENTATION_FINISH_PASS_STATUSES
        or str(immutable_base.get("status") or "").strip().lower()
        not in {
            "accepted_with_known_baseline_failure",
            "expected_red",
            "known_baseline_failure",
        }
    ):
        return False

    def _identity_set(
        container: Mapping[str, Any],
        *fields: str,
    ) -> tuple[bool, frozenset[str]]:
        found = False
        resolved: frozenset[str] | None = None
        for field in fields:
            if field not in container:
                continue
            found = True
            raw = container.get(field)
            if not isinstance(raw, list):
                return True, frozenset()
            values = [str(item or "").strip() for item in raw]
            if any(not item for item in values) or len(set(values)) != len(values):
                return True, frozenset()
            current = frozenset(values)
            if resolved is not None and current != resolved:
                return True, frozenset()
            resolved = current
        return found, resolved or frozenset()

    focused_ids_present, focused_ids = _identity_set(
        focused,
        "failure_identities",
        "failure_ids",
        "failed_test_ids",
    )
    expanded_ids_present, expanded_ids = _identity_set(
        expanded,
        "failure_identities",
        "failure_ids",
        "failed_test_ids",
    )
    base_ids_present, base_ids = _identity_set(
        immutable_base,
        "failure_identities",
        "failure_ids",
        "failed_test_ids",
    )
    if (
        focused_ids_present or expanded_ids_present or base_ids_present
    ) and (
        not focused_ids_present
        or not expanded_ids_present
        or not base_ids_present
        or focused_ids
        or expanded_ids
        or not base_ids
        or len(base_ids) != base_failed
    ):
        return False

    direct_identity_groups = []
    for fields in (
        (
            "candidate_failure_identities",
            "candidate_failure_ids",
            "full_failure_identities",
            "full_failure_ids",
        ),
        ("inherited_failure_identities", "inherited_failure_ids"),
        (
            "base_failure_identities",
            "base_failure_ids",
            "baseline_failure_identities",
            "baseline_failure_ids",
        ),
    ):
        present, identities = _identity_set(value, *fields)
        if present:
            if not identities:
                return False
            direct_identity_groups.append(identities)
    return not direct_identity_groups or bool(
        len(direct_identity_groups) == 3
        and all(group == direct_identity_groups[0] for group in direct_identity_groups)
        and len(direct_identity_groups[0]) == base_failed
        and (not base_ids_present or direct_identity_groups[0] == base_ids)
    )


def worker_implementation_test_results_validation(
    value: Any,
    *,
    evidence_envelope: bool = False,
) -> dict[str, Any]:
    """Return one structured verdict for implementation accept and commit.

    ``passed`` is a verdict, never a count.  Python's ``bool`` is a subclass of
    ``int``, so an ordinary truthiness check would otherwise accept the live
    deadlock shape ``{"passed": 1}`` at implementation time and reject it at
    worker-commit time.  When an evidence envelope is supplied, top-level and
    payload projections are aliases of the same evidence and therefore must be
    exactly equal rather than resolved by precedence.

    The returned mapping is intentionally copy-safe and field-specific so the
    server can use the same decision at both write boundaries without
    reinterpreting it.
    """

    schema_version = "contract_runtime.worker_implementation_test_results_validation.v1"
    if evidence_envelope:
        if not isinstance(value, Mapping):
            return {
                "schema_version": schema_version,
                "accepted": False,
                "canonical_test_results": {},
                "field": "worker_implementation",
                "reason": "evidence_envelope_must_be_mapping",
                "remediation": "submit a worker_implementation object",
            }
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        top_present = "test_results" in value
        nested_present = "test_results" in payload
        if not top_present and not nested_present:
            return {
                "schema_version": schema_version,
                "accepted": False,
                "canonical_test_results": {},
                "field": "test_results",
                "reason": "test_results_missing",
                "remediation": (
                    "submit explicit top-level status and a JSON boolean verdict"
                ),
            }
        top_value = value.get("test_results") if top_present else None
        nested_value = payload.get("test_results") if nested_present else None
        if top_present and nested_present and (
            not isinstance(top_value, Mapping)
            or not isinstance(nested_value, Mapping)
            or dict(top_value) != dict(nested_value)
        ):
            return {
                "schema_version": schema_version,
                "accepted": False,
                "canonical_test_results": {},
                "field": "test_results",
                "reason": "top_level_payload_test_results_conflict",
                "remediation": (
                    "submit one test_results object or make both aliases exactly equal"
                ),
            }
        value = top_value if top_present else nested_value

    def _reject(
        field: str,
        reason: str,
        remediation: str,
        *,
        received_type: str = "",
    ) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "accepted": False,
            "canonical_test_results": {},
            "field": field,
            "reason": reason,
            "remediation": remediation,
            "received_type": received_type,
        }

    if not isinstance(value, Mapping) or not value:
        return _reject(
            "test_results",
            "test_results_must_be_nonempty_mapping",
            "submit explicit structured test_results",
            received_type=(
                type(value).__name__ if value is not None else "missing"
            ),
        )
    if _worker_implementation_test_results_contains_raw_credential_field(value):
        return _reject(
            "test_results",
            "raw_credential_shaped_field_forbidden",
            "remove raw session, route, fence, token, secret, or credential fields",
            received_type=type(value).__name__,
        )
    canonical = deepcopy(dict(value))
    status = str(value.get("status") or "").strip().lower()
    if "passed" in value and not isinstance(value.get("passed"), bool):
        return _reject(
            "test_results.passed",
            "passed_must_be_json_boolean",
            "replace count-shaped passed with JSON true or false",
            received_type=type(value.get("passed")).__name__,
        )
    bounded_legacy_candidate_base = (
        _worker_implementation_legacy_results_finish_compatible(value)
    )
    if not status and bounded_legacy_candidate_base:
        return {
            "schema_version": schema_version,
            "accepted": True,
            "canonical_test_results": canonical,
            "field": "",
            "reason": "accepted",
            "remediation": "",
            "result_kind": "bounded_legacy_candidate_base",
        }
    if not status:
        if value.get("passed") is True:
            return {
                "schema_version": schema_version,
                "accepted": True,
                "canonical_test_results": canonical,
                "field": "",
                "reason": "accepted",
                "remediation": "",
                "result_kind": "legacy_owned_lane_boolean_pass",
            }
        return _reject(
            "test_results.status",
            "top_level_status_required",
            "set status to the exact owned-lane outcome",
            received_type="missing",
        )

    accepted = False
    result_kind = ""
    if status == _WORKER_IMPLEMENTATION_UNRELATED_BLOCK_STATUS:
        accepted = _worker_implementation_unrelated_block_finish_compatible(value)
        result_kind = "owned_lane_pass_with_unrelated_block"
    elif status == _WORKER_IMPLEMENTATION_NO_PASS_STATUS:
        accepted = _worker_implementation_known_baseline_finish_compatible(value)
        result_kind = "known_baseline_no_pass"
    elif status in _WORKER_IMPLEMENTATION_FINISH_AMBIGUOUS_STATUSES:
        return _reject(
            "test_results.status",
            "ambiguous_or_nonterminal_status",
            "record the exact terminal owned-lane outcome",
        )
    elif status in _WORKER_IMPLEMENTATION_FINISH_PASS_STATUSES:
        accepted = value.get("passed") is not False
        result_kind = "owned_lane_pass"
    elif bounded_legacy_candidate_base:
        accepted = True
        result_kind = "bounded_legacy_candidate_base"

    if not accepted:
        return _reject(
            "test_results",
            "test_results_not_finish_compatible",
            "use the copy-safe worker implementation test-results guide",
        )
    return {
        "schema_version": schema_version,
        "accepted": True,
        "canonical_test_results": canonical,
        "field": "",
        "reason": "accepted",
        "remediation": "",
        "result_kind": result_kind,
    }


def worker_implementation_legacy_accept_commit_divergence(
    value: Any,
    *,
    evidence_envelope: bool = False,
) -> dict[str, Any]:
    """Classify only the historical truthiness/identity deadlock shape.

    The old facade treated ``passed`` by truthiness while worker_commit used
    ``is True``.  This recovery is deliberately limited to the audited count
    verdicts ``{"passed": 1}`` and ``{"passed": 1, "failed": 0}`` accepted by
    that mismatch.  It is not a generic path for failed, partial, ambiguous,
    secret-bearing, or missing evidence.
    """

    schema_version = (
        "contract_runtime.worker_implementation_legacy_accept_commit_divergence.v1"
    )
    candidate = value
    if evidence_envelope:
        if not isinstance(value, Mapping):
            candidate = None
        else:
            payload = (
                value.get("payload")
                if isinstance(value.get("payload"), Mapping)
                else {}
            )
            top_present = "test_results" in value
            nested_present = "test_results" in payload
            top_value = value.get("test_results") if top_present else None
            nested_value = payload.get("test_results") if nested_present else None
            if (
                not (top_present or nested_present)
                or (
                    top_present
                    and nested_present
                    and (
                        not isinstance(top_value, Mapping)
                        or not isinstance(nested_value, Mapping)
                        or dict(top_value) != dict(nested_value)
                    )
                )
            ):
                candidate = None
            else:
                candidate = top_value if top_present else nested_value
    authorized = bool(
        isinstance(candidate, Mapping)
        and set(candidate) in ({"passed"}, {"passed", "failed"})
        and type(candidate.get("passed")) is int
        and candidate.get("passed") == 1
        and (
            "failed" not in candidate
            or (
                type(candidate.get("failed")) is int
                and candidate.get("failed") == 0
            )
        )
        and not _worker_implementation_test_results_contains_raw_credential_field(
            candidate
        )
    )
    return {
        "schema_version": schema_version,
        "authorized": authorized,
        "classification": (
            "legacy_accept_commit_divergence_passed_int_one"
            if authorized
            else "not_legacy_accept_commit_divergence"
        ),
        "field": "test_results.passed" if authorized else "test_results",
        "copy_safe": True,
        "generic_invalid_source_repair_allowed": False,
    }


def _worker_implementation_unrelated_block_finish_compatible(
    value: Mapping[str, Any],
) -> bool:
    """Validate an owned-lane pass with separately recorded unrelated blocks."""

    if (
            ("no_pass" in value and value.get("no_pass") is not True)
            or ("passed" in value and value.get("passed") is not False)
            or (
                "overall_release_pass" in value
                and value.get("overall_release_pass") is not False
            )
            or (
                "overall_release_pass_claimed" in value
                and value.get("overall_release_pass_claimed") is not False
            )
    ):
        return False
    required_passed = value.get("required_passed")
    unrelated_blocks = value.get("unrelated_system_blocks")
    tests = value.get("tests")
    if (
            not isinstance(required_passed, int)
            or isinstance(required_passed, bool)
            or required_passed <= 0
            or not isinstance(unrelated_blocks, int)
            or isinstance(unrelated_blocks, bool)
            or unrelated_blocks <= 0
            or not isinstance(tests, list)
            or not tests
    ):
        return False
    passed_count = 0
    blocked_count = 0
    for test in tests:
        if (
                not isinstance(test, Mapping)
                or not str(test.get("name") or "").strip()
                or not str(test.get("command") or "").strip()
        ):
            return False
        test_status = str(test.get("status") or "").strip().lower()
        if test_status in _WORKER_IMPLEMENTATION_FINISH_PASS_STATUSES:
            passed_count += 1
        elif test_status == "blocked_unrelated" and str(
            test.get("detail") or ""
        ).strip():
            blocked_count += 1
        else:
            return False
    return bool(
        passed_count == required_passed
        and blocked_count == unrelated_blocks
        and len(tests) == passed_count + blocked_count
    )


def _worker_implementation_known_baseline_finish_compatible(
    value: Mapping[str, Any],
) -> bool:
    """Validate the bounded no-pass shape for immutable baseline failures."""

    status = str(value.get("status") or "").strip().lower()
    if (
            status != _WORKER_IMPLEMENTATION_NO_PASS_STATUS
            or value.get("no_pass") is not True
            or ("passed" in value and value.get("passed") is not False)
            or (
                "overall_release_pass" in value
                and value.get("overall_release_pass") is not False
            )
            or (
                "overall_release_pass_claimed" in value
                and value.get("overall_release_pass_claimed") is not False
            )
    ):
        return False
    counts: dict[str, int] = {}
    for field in _WORKER_IMPLEMENTATION_NO_PASS_COUNT_FIELDS:
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool):
            return False
        counts[field] = count
    return bool(
        counts["candidate_new_failures"] == 0
        and counts["full_failed"] > 0
        and counts["full_failed"]
        == counts["inherited_failed"]
        == counts["baseline_failed"]
        and counts["focused_passed"] > 0
        and counts["full_passed"] >= counts["baseline_passed"] >= 0
    )


def _worker_implementation_test_results_finish_compatible(value: Any) -> bool:
    """Compatibility wrapper over the canonical structured validator."""

    return bool(worker_implementation_test_results_validation(value)["accepted"])


def _worker_implementation_atomic_advance_errors(
    record: Mapping[str, Any],
    written_line: Mapping[str, Any],
) -> list[str]:
    """Reject an implementation before persistence if its lane cannot advance.

    The implementation facade is one atomic action: an accepted canonical line
    must appear in the authoritative compiled completion set, and the same lane
    may not remain on ``worker_implementation``. A sibling lane may still be
    the next implementation instance in a multi-worker contract.
    """

    if str(written_line.get("line_id") or "").strip() != "worker_implementation":
        return []
    expected_instance = str(
        written_line.get("line_instance_id")
        or f"runtime_context:{_worker_commit_text(written_line, 'runtime_context_id')}"
    ).strip()
    state = (
        record.get("execution_state")
        if isinstance(record.get("execution_state"), Mapping)
        else {}
    )
    completed = state.get("completed_lines")
    completed = completed if isinstance(completed, list) else []
    completion_consumed = any(
        isinstance(item, Mapping)
        and str(item.get("stage_id") or "").strip()
        == str(written_line.get("stage_id") or "").strip()
        and str(item.get("line_id") or "").strip()
        == "worker_implementation"
        and str(item.get("line_instance_id") or "").strip()
        == expected_instance
        for item in completed
    )
    guide = (
        record.get("runtime_guide")
        if isinstance(record.get("runtime_guide"), Mapping)
        else {}
    )
    next_action = (
        guide.get("next_legal_action")
        if isinstance(guide.get("next_legal_action"), Mapping)
        else {}
    )
    same_lane_still_current = bool(
        str(next_action.get("stage_id") or "").strip()
        == str(written_line.get("stage_id") or "").strip()
        and str(next_action.get("line_id") or "").strip()
        == "worker_implementation"
        and str(next_action.get("line_instance_id") or "").strip()
        == expected_instance
    )
    errors: list[str] = []
    if not completion_consumed:
        errors.append("worker_implementation_not_completion_satisfying")
    if same_lane_still_current:
        errors.append("worker_implementation_atomic_lane_not_advanced")
    return errors


def _worker_commit_has_finish_compatible_implementation_results(
    implementation: Mapping[str, Any],
    *,
    record: Mapping[str, Any] | None = None,
    correction: Mapping[str, Any] | None = None,
) -> bool:
    direct = worker_implementation_test_results_validation(
        implementation,
        evidence_envelope=True,
    )
    if direct["accepted"]:
        return True
    if not isinstance(record, Mapping) or not isinstance(correction, Mapping):
        return False
    return bool(
        worker_implementation_test_results_correction_validation(
            record,
            implementation,
            correction,
        )["accepted"]
    )


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
    fence_containment = _worker_fence_containment(
        sorted(changed_files),
        sorted(owned_files),
        repository_root=resolved.get("target_project_root", ""),
    )
    out_of_fence = list(fence_containment["out_of_fence_files"])
    if not fence_containment["ok"]:
        errors.append(f"worker_commit contains out-of-fence files: {out_of_fence!r}")

    normal_target_revision: Mapping[str, Any] = {}
    for candidate in _worker_commit_mapping_candidates(write):
        if str(candidate.get("schema_version") or "").strip() == (
            "runtime_context.normal_pre_qa_target_head_revision.v1"
        ):
            normal_target_revision = candidate
            break
    if normal_target_revision:
        normal_worker_files = {
            str(item or "").strip()
            for item in normal_target_revision.get(
                "worker_authored_candidate_delta_files"
            )
            or []
            if str(item or "").strip()
        }
        normal_diff_base = str(
            normal_target_revision.get("current_target_baseline_commit")
            or ""
        ).strip()
        recorded_diff_base = _worker_commit_text(write, "diff_base_commit")
        target_boundary_applied = bool(
            normal_target_revision.get("target_head_boundary_applied")
        )
        runtime_target = str(
            normal_target_revision.get("runtime_target_head_commit") or ""
        ).strip()
        if (
            str(normal_target_revision.get("source") or "").strip()
            != "server_revalidated_normal_pre_qa_target_head"
            or normal_target_revision.get("server_derived") is not True
            or normal_target_revision.get(
                "post_qa_merge_conflict_recovery"
            )
            is not False
        ):
            errors.append(
                "worker_commit normal pre-QA target projection requires "
                "server-derived non-post-QA authority"
            )
        if normal_worker_files != changed_files:
            errors.append(
                "worker_commit normal pre-QA worker delta does not match "
                "changed_files"
            )
        if not normal_diff_base or normal_diff_base != recorded_diff_base:
            errors.append(
                "worker_commit normal pre-QA target baseline does not match "
                "diff_base_commit"
            )
        if target_boundary_applied and runtime_target != normal_diff_base:
            errors.append(
                "worker_commit applied target boundary must equal the runtime "
                "target HEAD"
            )
        if normal_target_revision.get(
            "target_baseline_changes_worker_authored"
        ) is not False:
            errors.append(
                "worker_commit inherited target files cannot be worker-authored"
            )

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
        implementation_lineage = _worker_implementation_lineage(
            record,
            implementation,
        )
        write_payload = (
            write.get("payload")
            if isinstance(write.get("payload"), Mapping)
            else {}
        )
        correction_projection = (
            write.get("worker_implementation_test_results_correction")
            if isinstance(
                write.get("worker_implementation_test_results_correction"),
                Mapping,
            )
            else write_payload.get(
                "worker_implementation_test_results_correction"
            )
            if isinstance(
                write_payload.get(
                    "worker_implementation_test_results_correction"
                ),
                Mapping,
            )
            else None
        )
        correction_validation = (
            worker_implementation_test_results_correction_validation(
                record,
                implementation,
                correction_projection,
            )
            if isinstance(correction_projection, Mapping)
            else {}
        )
        if not _worker_commit_has_finish_compatible_implementation_results(
            implementation,
            record=record,
            correction=correction_projection,
        ):
            errors.append(
                "worker_commit requires canonical worker_implementation "
                "finish-compatible test_results"
            )
        supplied_lineage_ref = _worker_commit_text(
            write,
            "implementation_lineage_ref",
        )
        if (
            supplied_lineage_ref
            and supplied_lineage_ref
            != implementation_lineage["implementation_lineage_ref"]
        ):
            errors.append(
                "worker_commit implementation_lineage_ref does not match "
                "canonical worker_implementation"
            )
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
        if correction_validation.get("accepted") is True:
            direct_write_graph_trace_ids = write.get("graph_trace_ids")
            if not isinstance(direct_write_graph_trace_ids, list):
                direct_write_graph_trace_ids = write_payload.get(
                    "graph_trace_ids"
                )
            graph_trace_ids = {
                str(item or "").strip()
                for item in (
                    direct_write_graph_trace_ids
                    if isinstance(direct_write_graph_trace_ids, list)
                    else []
                )
                if str(item or "").strip()
            }
            implementation_graph_trace_ids = set(graph_trace_ids)
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


def _mf_parallel_atomic_lane_gate_view(
    definition: Mapping[str, Any],
    record: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
    write: Mapping[str, Any],
    *,
    completion_satisfying_lines: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind one mf_sub write to its own first missing atomic-dispatch line.

    The persisted execution state remains the deterministic global scheduler
    view.  This private gate view only replaces the next action for an exact,
    allocated RuntimeContext while validating its write.  Observer-owned lines
    (especially ``observer_merge``) therefore retain the global all-lanes
    barrier.
    """

    state = (
        dict(record.get("execution_state"))
        if isinstance(record.get("execution_state"), Mapping)
        else {}
    )
    guide = dict(runtime_guide)
    if (
        str(definition.get("contract_id") or "")
        not in _MF_PARALLEL_CONTRACT_IDS
        or str(write.get("actor_role") or "").strip() != "mf_sub"
    ):
        return state, guide

    completed_lines = [
        item
        for item in completion_satisfying_lines
        if isinstance(item, Mapping)
    ]
    lane_dispatches: list[Mapping[str, Any]] = []
    for item in completed_lines:
        if (
            str(item.get("stage_id") or "").strip(),
            str(item.get("line_id") or "").strip(),
        ) != _MF_PARALLEL_ATOMIC_DISPATCH_LINE:
            continue
        lane_dispatches.append(item)
    if len(lane_dispatches) != 1:
        return state, guide

    dispatch = lane_dispatches[0]
    dispatch_payload = (
        dispatch.get("payload")
        if isinstance(dispatch.get("payload"), Mapping)
        else {}
    )
    try:
        required_worker_count = int(
            dispatch_payload.get("required_worker_count") or 0
        )
        worker_count = int(dispatch_payload.get("worker_count") or 0)
    except (TypeError, ValueError):
        return state, guide
    atomic_dispatch = dispatch_payload.get("atomic_dispatch") is True
    all_or_nothing = dispatch_payload.get("all_or_nothing") is True
    cardinality_contract_valid = (
        required_worker_count == 2
        and atomic_dispatch
        and all_or_nothing
    ) or (
        required_worker_count == 1
        and not atomic_dispatch
        and not all_or_nothing
    )
    workers = _mf_parallel_worker_instances([dispatch])
    raw_workers = list(_iter_worker_payloads(dispatch_payload))
    runtime_context_ids = [
        str(worker.get("runtime_context_id") or "").strip()
        for worker in workers
    ]
    raw_worker_ids = [
        _first_mapping_text(
            worker,
            "worker_id",
            nested_keys=("runtime_context", "context", "branch_context"),
        )
        for worker in raw_workers
    ]
    required_identity_fields = (
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "lane_id",
        "worker_slot_id",
        "line_instance_id",
    )
    if (
        not cardinality_contract_valid
        or worker_count != required_worker_count
        or len(workers) != required_worker_count
        or len(raw_workers) != required_worker_count
        or len(set(runtime_context_ids)) != required_worker_count
        or not all(runtime_context_ids)
        or not all(raw_worker_ids)
        or len(set(raw_worker_ids)) != required_worker_count
        or any(
            not all(str(worker.get(field) or "").strip() for field in required_identity_fields)
            for worker in workers
        )
        or any(
            len(
                {
                    str(worker.get(field) or "").strip()
                    for worker in workers
                }
            )
            != required_worker_count
            for field in (
                "runtime_context_id",
                "task_id",
                "lane_id",
                "worker_slot_id",
                "line_instance_id",
            )
        )
    ):
        return state, guide

    requested_runtime_context_id = _first_mapping_text(
        write,
        "runtime_context_id",
        nested_keys=("payload",),
    )
    matching_workers = [
        worker
        for worker in workers
        if str(worker.get("runtime_context_id") or "").strip()
        == requested_runtime_context_id
    ]
    if len(matching_workers) != 1:
        return state, guide
    worker = dict(matching_workers[0])
    raw_matching_workers = [
        item
        for item in raw_workers
        if _first_mapping_text(
            item,
            "runtime_context_id",
            "runtimeContextId",
            nested_keys=("runtime_context", "context", "branch_context"),
        )
        == requested_runtime_context_id
    ]
    if len(raw_matching_workers) != 1:
        return state, guide
    worker_id = _first_mapping_text(
        raw_matching_workers[0],
        "worker_id",
        nested_keys=("runtime_context", "context", "branch_context"),
    )
    if worker_id:
        worker["worker_id"] = worker_id
    line_instance_id = str(worker.get("line_instance_id") or "").strip()
    if not line_instance_id:
        return state, guide

    completed = {
        (
            str(item.get("stage_id") or "").strip(),
            str(item.get("line_id") or "").strip(),
            _line_instance_id_from_mapping(item),
        )
        for item in completed_lines
    }
    lane_next_action: dict[str, Any] | None = None
    for stage, line in iter_stage_lines(definition):
        owner_role = str(line.get("owner_role") or "").strip()
        allowed_roles = {
            str(role or "").strip()
            for role in (line.get("allowed_writer_roles") or [])
        }
        if (
            not _is_mf_parallel_lane_line(definition, line)
            or owner_role != "mf_sub"
            or "mf_sub" not in allowed_roles
        ):
            continue
        stage_id = str(stage.get("stage_id") or "").strip()
        line_id = str(line.get("line_id") or "").strip()
        if (stage_id, line_id, line_instance_id) in completed:
            continue
        lane_next_action = {
            "stage_id": stage_id,
            "line_id": line_id,
            "atomic_lane_gate_bound": True,
            "owner_role": owner_role,
            "allowed_writer_roles": list(line.get("allowed_writer_roles") or []),
            "evidence_kind": str(line.get("evidence_kind") or ""),
            "required": bool(line.get("required", True)),
            **dict(worker),
        }
        break

    source_global_runtime_guide_hash = str(
        guide.get("runtime_guide_hash") or ""
    ).strip()
    lane_binding = {
        "schema_version": "mf_parallel.atomic_lane_gate_binding.v1",
        "bound": True,
        **dict(worker),
    }
    state["atomic_lane_gate_binding"] = lane_binding
    state["next_action"] = lane_next_action
    state["execution_state_hash"] = stable_sha256(
        {
            key: value
            for key, value in state.items()
            if key != "execution_state_hash"
        }
    )
    guide["next_legal_action"] = (
        dict(lane_next_action) if lane_next_action is not None else None
    )
    guide["atomic_lane_gate_binding"] = {
        **lane_binding,
        "source_global_runtime_guide_hash": (
            source_global_runtime_guide_hash
        ),
    }
    safe_copy = (
        dict(guide.get("writer_role_safe_copy_payload") or {})
        if isinstance(guide.get("writer_role_safe_copy_payload"), Mapping)
        else {}
    )
    guide.pop("writer_role_safe_copy_payload", None)
    lane_hash_authority = {
        "schema_version": "contract_runtime.atomic_lane_guide_authority.v1",
        "contract": {
            "contract_id": str(definition.get("contract_id") or ""),
            "version": str(definition.get("version") or ""),
            "revision": str(definition.get("revision") or ""),
            "definition_hash": str(
                definition.get("definition_hash") or ""
            ),
        },
        "execution": {
            "project_id": str(record.get("project_id") or ""),
            "backlog_id": str(record.get("backlog_id") or ""),
            "contract_execution_id": str(
                record.get("contract_execution_id") or ""
            ),
            "execution_state_revision": int(
                record.get("execution_state_revision") or 0
            ),
        },
        "atomic_dispatch_hash": stable_sha256(dispatch),
        "lane_binding": lane_binding,
        "next_legal_action": (
            dict(lane_next_action)
            if lane_next_action is not None
            else None
        ),
    }
    guide["runtime_guide_hash_scope"] = {
        "schema_version": "contract_runtime.atomic_lane_guide_hash_scope.v1",
        "source": "mf_parallel_atomic_lane_gate_view",
        "private_writer_lane": True,
        "hash_input": "authority",
        "authority": lane_hash_authority,
        "source_global_runtime_guide_hash": (
            source_global_runtime_guide_hash
        ),
    }
    lane_runtime_guide_hash = stable_sha256(lane_hash_authority)
    guide["runtime_guide_hash"] = lane_runtime_guide_hash
    if lane_next_action is None:
        return state, guide

    copy_payload = (
        dict(safe_copy.get("copy_payload") or {})
        if isinstance(safe_copy.get("copy_payload"), Mapping)
        else {}
    )
    if copy_payload:
        copy_payload.update(
            {
                "stage_id": lane_next_action["stage_id"],
                "line_id": lane_next_action["line_id"],
                "actor_role": "mf_sub",
                "evidence_kind": lane_next_action["evidence_kind"],
                "execution_state_revision": int(
                    state.get("execution_state_revision") or 0
                ),
                "runtime_guide_hash": lane_runtime_guide_hash,
            }
        )
        for field in (
            "line_instance_id",
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "worker_role",
            "lane_id",
            "worker_slot_id",
            "worker_id",
        ):
            value = lane_next_action.get(field)
            if value not in (None, ""):
                copy_payload[field] = value
            else:
                copy_payload.pop(field, None)
        safe_copy["copy_payload"] = copy_payload
        alignment = (
            dict(safe_copy.get("hash_alignment") or {})
            if isinstance(safe_copy.get("hash_alignment"), Mapping)
            else {}
        )
        alignment["required_owner_role"] = "mf_sub"
        alignment["required_writer_role"] = "mf_sub"
        alignment["reader_runtime_guide_hash"] = (
            source_global_runtime_guide_hash
        )
        alignment["required_writer_runtime_guide_hash"] = (
            lane_runtime_guide_hash
        )
        alignment["reader_hash_is_writer_hash"] = (
            source_global_runtime_guide_hash
            == lane_runtime_guide_hash
        )
        safe_copy["hash_alignment"] = alignment
        guide["writer_role_safe_copy_payload"] = safe_copy
    return state, guide


def _bind_mf_parallel_atomic_lane_runtime_guide_hash(
    write: dict[str, Any],
    runtime_guide: Mapping[str, Any],
) -> None:
    """Exchange one current global scheduler hash for its exact lane hash.

    Direct ``ContractRuntime`` callers read the persisted global guide.  Once
    the server deterministically binds their exact atomic worker identity to a
    private lane view, the same current global hash is an admissible revision
    token and is exchanged for the content-bound private guide hash.  Missing,
    arbitrary, or stale hashes are never repaired here and remain fail-closed.
    """

    binding = (
        runtime_guide.get("atomic_lane_gate_binding")
        if isinstance(
            runtime_guide.get("atomic_lane_gate_binding"),
            Mapping,
        )
        else {}
    )
    if binding.get("bound") is not True:
        return
    source_hash = str(
        binding.get("source_global_runtime_guide_hash") or ""
    ).strip()
    lane_hash = str(runtime_guide.get("runtime_guide_hash") or "").strip()
    requested_hash = str(write.get("runtime_guide_hash") or "").strip()
    if source_hash and lane_hash and requested_hash == source_hash:
        write["runtime_guide_hash"] = lane_hash


def _line_shape_allows_contract_completion(line: Mapping[str, Any]) -> bool:
    line_id = str(line.get("line_id") or "").strip()
    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    schema_version = str(payload.get("schema_version") or "").strip()
    blocked_schemas = _LINE_PAYLOAD_SCHEMA_BLOCKLIST.get(line_id)
    return not (blocked_schemas and schema_version in blocked_schemas)


def _contains_completion_blocker_outside_baseline_observation(
    value: Any,
) -> bool:
    """Keep explicit frozen-baseline observations out of the QA verdict.

    A ``baseline_observation`` mapping may carry the non-zero failure count it
    observed.  That narrow container-local count is audit evidence, not a QA
    FAIL.  Blocking fields outside that exact mapping, including siblings and
    nested children, remain authoritative.
    """

    if isinstance(value, Mapping):
        baseline_observation = (
            str(value.get("status") or "").strip().lower()
            == "baseline_observation"
        )
        for raw_key, item in value.items():
            key = str(raw_key or "").strip().lower()
            if (
                key in _CONTRACT_COMPLETION_STATUS_FIELDS
                and str(item or "").strip().lower()
                in _CONTRACT_COMPLETION_BLOCKING_STATUSES
            ):
                return True
            if (
                key in _CONTRACT_COMPLETION_FAILURE_COUNT_FIELDS
                and _truthy_failure_count(item)
                and not baseline_observation
            ):
                return True
            if _contains_completion_blocker_outside_baseline_observation(
                item
            ):
                return True
        return False
    if isinstance(value, list):
        return any(
            _contains_completion_blocker_outside_baseline_observation(item)
            for item in value
        )
    return False


def _authenticated_qa_pass_with_baseline_observations(
    line: Mapping[str, Any],
) -> bool:
    """Recognize only server-bound QA PASS with observation-local failures."""

    provenance = (
        line.get("qa_evidence_provenance")
        if isinstance(line.get("qa_evidence_provenance"), Mapping)
        else {}
    )
    binding = (
        provenance.get("authenticated_qa_binding")
        if isinstance(provenance.get("authenticated_qa_binding"), Mapping)
        else {}
    )
    status_gate = (
        provenance.get("completion_status_gate")
        if isinstance(provenance.get("completion_status_gate"), Mapping)
        else {}
    )
    qa_status = str(line.get("status") or "").strip().lower()
    return bool(
        str(line.get("line_id") or "").strip()
        == "qa_independent_verification"
        and str(line.get("actor_role") or "").strip().lower() == "qa"
        and str(line.get("evidence_kind") or "").strip()
        == "independent_verification"
        and qa_status in _QA_COMPLETION_PASSING_STATUSES
        and str(line.get("authorization_source") or "")
        == "qa_session_token_ref"
        and line.get("observer_impersonation") is False
        and line.get("parent_materialization_authorized") is False
        and str(provenance.get("schema_version") or "")
        == "qa_evidence_provenance.v1"
        and provenance.get("server_derived") is True
        and str(provenance.get("authorization_source") or "")
        == "qa_session_token_ref"
        and str(provenance.get("evidence_owner_role") or "") == "qa"
        and provenance.get("observer_impersonation") is False
        and provenance.get("parent_materialization_authorized") is False
        and str(binding.get("schema_version") or "")
        == "contract_runtime.authenticated_qa_binding.v1"
        and binding.get("server_derived") is True
        and binding.get("independent_verification_session_matched") is True
        and str(binding.get("qa_principal") or "").strip()
        and str(binding.get("qa_session_id") or "").strip()
        and str(status_gate.get("schema_version") or "")
        == _QA_COMPLETION_STATUS_GATE_SCHEMA_VERSION
        and status_gate.get("server_derived") is True
        and status_gate.get("top_level_status_present") is True
        and status_gate.get("top_level_status_passing") is True
        and str(status_gate.get("normalized_status") or "") == qa_status
        and status_gate.get("nested_payload_decision_satisfies") is False
        and not _contains_completion_blocker_outside_baseline_observation(
            line
        )
        and not _qa_independent_verification_summary_reports_failure(line)
    )


def _line_status_allows_contract_completion(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None = None,
    source_line_index: int = -1,
) -> bool:
    if _mapping_own_fields_contain_contract_completion_blocker(line):
        return False
    line_id = str(line.get("line_id") or "").strip()
    canonical_rework_baseline = bool(
        line_id == "worker_implementation"
        and _worker_implementation_canonical_baseline_completion(
            line,
            source_record=source_record,
            source_line_index=source_line_index,
        )
    )
    canonical_owned_lane_nonrelease = bool(
        line_id == "worker_implementation"
        and _worker_implementation_canonical_owned_lane_nonrelease_completion(
            line,
            source_record=source_record,
            source_line_index=source_line_index,
        )
    )
    canonical_no_pass = bool(
        line_id == "qa_independent_verification"
        and _qa_independent_verification_canonical_no_pass_completion(
            line,
            source_record=source_record,
            source_line_index=source_line_index,
        )
    )
    authenticated_observation_pass = bool(
        line_id == "qa_independent_verification"
        and _authenticated_qa_pass_with_baseline_observations(line)
    )
    verification_evidence: Any = line.get("verification")
    if line_id == "worker_implementation":
        payload = (
            line.get("payload")
            if isinstance(line.get("payload"), Mapping)
            else {}
        )
        verification_evidence = {
            "top_level": line.get("verification"),
            "payload": payload.get("verification"),
        }
    if _contains_contract_completion_blocker(line.get("qa_evidence_provenance")):
        return False
    if (
        not (
            canonical_no_pass
            or canonical_rework_baseline
            or canonical_owned_lane_nonrelease
            or authenticated_observation_pass
        )
        and _contains_contract_completion_blocker(verification_evidence)
    ):
        return False
    if line_id == "qa_independent_verification":
        provenance = (
            line.get("qa_evidence_provenance")
            if isinstance(line.get("qa_evidence_provenance"), Mapping)
            else {}
        )
        status_gate = (
            provenance.get("completion_status_gate")
            if isinstance(provenance.get("completion_status_gate"), Mapping)
            else {}
        )
        if (
            str(status_gate.get("schema_version") or "")
            == _QA_COMPLETION_STATUS_GATE_SCHEMA_VERSION
        ):
            qa_status = str(line.get("status") or "").strip().lower()
            if (
                status_gate.get("top_level_status_present") is not True
                or status_gate.get("top_level_status_passing") is not True
                or str(status_gate.get("normalized_status") or "") != qa_status
                or qa_status not in _QA_COMPLETION_PASSING_STATUSES
            ):
                return False
        if canonical_no_pass:
            return True
        # Ordinary independent-QA evidence is fail-closed across the entire
        # persisted line. Command/test summaries and artifact references are
        # first-class evidence containers that may contradict an otherwise
        # passing top-level status. The authenticated canonical no-PASS ledger
        # and explicit, container-local baseline observations are the only
        # narrow exceptions.
        if (
            not authenticated_observation_pass
            and _contains_contract_completion_blocker(line)
        ):
            return False
        if _qa_independent_verification_summary_reports_failure(line):
            return False
    return True


def _worker_implementation_canonical_owned_lane_nonrelease_completion(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None,
    source_line_index: int,
) -> bool:
    """Accept only the existing exact owned-lane non-release result shapes.

    An implementation may prove its owned requirements while also recording a
    separately bounded baseline or sibling-system block. Those two shapes are
    already validated by ``worker_implementation_test_results_validation``.
    Reuse that authority here instead of letting a nested diagnostic failure
    contradict the write Gate after the canonical line has been appended.
    Persisted source membership and authenticated worker provenance prevent a
    generic failure-bearing mapping from becoming completion authority.
    """

    if not isinstance(source_record, Mapping):
        return False
    persisted_index = _source_record_completed_line_index(line, source_record)
    if persisted_index < 0 or source_line_index != persisted_index:
        return False
    if (
        str(line.get("line_id") or "").strip() != "worker_implementation"
        or str(line.get("actor_role") or "").strip() != "mf_sub"
        or str(line.get("evidence_kind") or "").strip() != "implementation"
    ):
        return False
    validation = worker_implementation_test_results_validation(
        line,
        evidence_envelope=True,
    )
    if validation.get("accepted") is not True or str(
        validation.get("result_kind") or ""
    ) not in {
        "known_baseline_no_pass",
        "owned_lane_pass_with_unrelated_block",
    }:
        return False
    payload = (
        line.get("payload")
        if isinstance(line.get("payload"), Mapping)
        else {}
    )
    provenance = (
        payload.get("worker_evidence_provenance")
        if isinstance(payload.get("worker_evidence_provenance"), Mapping)
        else {}
    )
    return bool(
        str(provenance.get("schema_version") or "")
        == "runtime_context.worker_provenance.v1"
        and provenance.get("verified") is True
        and provenance.get("worker_owned") is True
        and provenance.get("observer_impersonation") is False
        and str(provenance.get("worker_role") or "") == "mf_sub"
        and str(provenance.get("runtime_context_id") or "").strip()
        == _worker_commit_text(line, "runtime_context_id")
        and str(provenance.get("task_id") or "").strip()
        == _worker_commit_text(line, "task_id")
    )


def _worker_implementation_canonical_baseline_completion(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None,
    source_line_index: int,
) -> bool:
    """Accept one persisted, server-verified failed-QA baseline rework line.

    Positive historical failures are completion-satisfying only when they are
    bound to the canonical failed-QA rework authority and exactly reproduce an
    immutable positive baseline with zero candidate-new failures.  The source
    membership requirement keeps generic counts-only implementation lines from
    becoming completion authority.
    """

    if not isinstance(source_record, Mapping):
        return False
    persisted_index = _source_record_completed_line_index(line, source_record)
    if persisted_index < 0 or source_line_index != persisted_index:
        return False
    lines = source_record.get("completed_lines")
    if not isinstance(lines, list) or persisted_index >= len(lines):
        return False

    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    revision = (
        payload.get("canonical_rework_lineage_revision")
        if isinstance(payload.get("canonical_rework_lineage_revision"), Mapping)
        else {}
    )
    authority = (
        payload.get("canonical_rework_lineage_revision_authority")
        if isinstance(
            payload.get("canonical_rework_lineage_revision_authority"), Mapping
        )
        else {}
    )
    rejoin = (
        payload.get("failed_qa_revision_rejoin_marker")
        if isinstance(payload.get("failed_qa_revision_rejoin_marker"), Mapping)
        else {}
    )
    graph = (
        payload.get("graph_trace_db_evidence")
        if isinstance(payload.get("graph_trace_db_evidence"), Mapping)
        else {}
    )
    provenance = (
        payload.get("worker_evidence_provenance")
        if isinstance(payload.get("worker_evidence_provenance"), Mapping)
        else {}
    )
    execution_id = str(source_record.get("contract_execution_id") or "").strip()
    runtime_context_id = _worker_commit_text(line, "runtime_context_id")
    task_id = _worker_commit_text(line, "task_id")
    parent_task_id = _worker_commit_text(line, "parent_task_id")
    commit_sha = str(line.get("commit_sha") or "").strip().lower()
    revision_event_ref = str(revision.get("revision_event_ref") or "").strip()
    try:
        failed_qa_index = int(revision.get("failed_qa_completed_line_index"))
    except (TypeError, ValueError):
        return False
    failed_qa_ref = (
        f"contract_runtime:{execution_id}:completed_lines:{failed_qa_index}"
    )
    if not (
        _record_contract_id(source_record) in {"mf_parallel", "mf_parallel.v2"}
        and execution_id
        and 0 <= failed_qa_index < persisted_index
        and str(lines[failed_qa_index].get("line_id") or "").strip()
        == "qa_independent_verification"
        and not _line_status_allows_contract_completion(
            lines[failed_qa_index],
            source_record=source_record,
            source_line_index=failed_qa_index,
        )
        and str(line.get("actor_role") or "").strip() == "mf_sub"
        and str(line.get("evidence_kind") or "").strip() == "implementation"
        and str(line.get("status") or "").strip().lower() == "completed"
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha)
        and str(revision.get("schema_version") or "")
        == "contract_runtime.worker_implementation_rework_revision.v1"
        and str(revision.get("source") or "")
        == "server_verified_failed_qa_rework"
        and revision.get("append_only_history_preserved") is True
        and str(revision.get("commit_sha") or "").strip().lower() == commit_sha
        and re.fullmatch(r"timeline:[1-9][0-9]*", revision_event_ref)
        and str(authority.get("schema_version") or "")
        == "runtime_context.clean_cumulative_git_revision_authority.v1"
        and str(authority.get("source") or "")
        == "runtime_context_clean_cumulative_git_revision"
        and authority.get("server_derived") is True
        and authority.get("clean_worktree") is True
        and str(authority.get("actual_head_commit") or "").strip().lower()
        == commit_sha
        and str(authority.get("revision_event_ref") or "").strip()
        == revision_event_ref
        and str(authority.get("failed_qa_source_ref") or "").strip()
        == failed_qa_ref
        and str(rejoin.get("schema_version") or "")
        == "contract_runtime.failed_qa_revision_rejoin_marker.v1"
        and str(rejoin.get("source") or "")
        == "accepted_runtime_context_rejoin_event"
        and rejoin.get("evidence_backfill") is False
        and str(rejoin.get("contract_execution_id") or "").strip()
        == execution_id
        and str(rejoin.get("runtime_context_id") or "").strip()
        == runtime_context_id
        and str(rejoin.get("task_id") or "").strip() == task_id
        and str(rejoin.get("parent_task_id") or "").strip() == parent_task_id
        and str(rejoin.get("revision_event_ref") or "").strip()
        == revision_event_ref
        and str(rejoin.get("failed_qa_source") or "")
        == "contract_runtime_completed_lines"
        and str(rejoin.get("failed_qa_source_ref") or "").strip()
        == failed_qa_ref
        and str(graph.get("schema_version") or "")
        == "mf_subagent_graph_trace_db_evidence.v1"
        and graph.get("db_verified") is True
        and not list(graph.get("missing_trace_ids") or [])
        and not list(graph.get("identity_mismatches") or [])
        and str(graph.get("query_source") or "") == "mf_subagent"
        and str(graph.get("worker_role") or "") == "mf_sub"
        and str(graph.get("runtime_context_id") or "").strip()
        == runtime_context_id
        and str(graph.get("task_id") or "").strip() == task_id
        and str(graph.get("parent_task_id") or "").strip() == parent_task_id
        and str(provenance.get("schema_version") or "")
        == "contract_runtime.worker_evidence_provenance.v1"
        and str(provenance.get("source") or "")
        == "runtime_context_copy_safe_worker_proof"
        and provenance.get("verified") is True
        and provenance.get("worker_owned") is True
        and str(provenance.get("worker_role") or "") == "mf_sub"
        and str(provenance.get("runtime_context_id") or "").strip()
        == runtime_context_id
        and str(provenance.get("task_id") or "").strip() == task_id
    ):
        return False

    trace_ids = _qa_no_pass_failure_identities(payload.get("graph_trace_ids"))
    verified_trace_ids = _qa_no_pass_failure_identities(
        graph.get("verified_trace_ids")
    )
    requested_trace_ids = _qa_no_pass_failure_identities(
        graph.get("requested_trace_ids")
    )
    if not (trace_ids and trace_ids == verified_trace_ids == requested_trace_ids):
        return False

    count_sources: list[Mapping[str, Any]] = []
    for raw_source in (
        line.get("test_results"),
        line.get("verification"),
        payload.get("test_results"),
        payload.get("verification"),
    ):
        if not isinstance(raw_source, Mapping):
            continue
        affected = raw_source.get("affected_suite")
        count_sources.append(
            affected if isinstance(affected, Mapping) else raw_source
        )
    for test in payload.get("tests") or []:
        if isinstance(test, Mapping) and (
            str(test.get("name") or "") == "affected_suite_baseline_compare"
            or str(test.get("status") or "") == "baseline_matched"
        ):
            count_sources.append(test)

    count_tuples: list[tuple[int, int, int]] = []
    baseline_identity_sets: list[tuple[str, ...]] = []
    candidate_identity_sets: list[tuple[str, ...]] = []
    for source in count_sources:
        count_tuple = _worker_implementation_baseline_counts(source)
        if count_tuple is not None:
            count_tuples.append(count_tuple)
        for key in (
            "baseline_failure_node_ids",
            "baseline_failure_identities",
            "base_failure_node_ids",
            "base_failure_identities",
        ):
            if key in source:
                baseline_identity_sets.append(
                    _qa_no_pass_failure_identities(source.get(key))
                )
        for key in (
            "candidate_failure_node_ids",
            "candidate_failure_identities",
        ):
            if key in source:
                candidate_identity_sets.append(
                    _qa_no_pass_failure_identities(source.get(key))
                )
    if not (
        len(count_tuples) >= 2
        and all(
            baseline > 0 and candidate == baseline and candidate_new == 0
            for baseline, candidate, candidate_new in count_tuples
        )
        and len(set(count_tuples)) == 1
    ):
        return False
    if baseline_identity_sets or candidate_identity_sets:
        if not (
            baseline_identity_sets
            and candidate_identity_sets
            and all(baseline_identity_sets)
            and all(candidate_identity_sets)
            and len(set(baseline_identity_sets + candidate_identity_sets)) == 1
        ):
            return False
    return True


def _worker_implementation_baseline_counts(
    value: Mapping[str, Any],
) -> tuple[int, int, int] | None:
    aliases = (
        ("baseline_failed", "base_failed"),
        ("candidate_failed", "affected_suite_failed", "failed"),
        (
            "candidate_new_failures",
            "candidate_specific_new_failures",
            "new_failures",
        ),
    )
    selected: list[Any] = []
    for names in aliases:
        selected.append(next((value[name] for name in names if name in value), None))
    if all(item is None for item in selected):
        return None
    if not all(
        isinstance(item, int) and not isinstance(item, bool)
        for item in selected
    ):
        return (-1, -1, -1)
    return tuple(selected)  # type: ignore[return-value]


_QA_NO_PASS_LEDGER_SCHEMA_VERSION = (
    "contract_runtime.external_no_pass_baseline_ledger.v2"
)
_QA_NO_PASS_PAYLOAD_SCHEMA_VERSIONS = frozenset(
    {
        "qa_independent_verification.v1",
        "mf_parallel.qa_independent_verification.v1",
    }
)
# The migration predates ``server_normalized`` on this one stored QA line.
# New lines must carry the marker and cannot opt into compatibility by shape.
_QA_NO_PASS_PRE_MARKER_PERSISTED_LINE_IDENTITIES = frozenset(
    {
        ("cex-mf-parallel-87fbdfddeff1876e8617", 10),
    }
)


def _qa_independent_verification_canonical_no_pass_completion(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None = None,
    source_line_index: int = -1,
) -> bool:
    """Recognize only the authenticated canonical no-PASS ledger.

    The generic completion blocker intentionally treats any nested non-zero
    failure count as failed QA.  This shape is the single exception: the
    candidate reproduces the exact non-empty baseline failures, adds none, and
    explicitly makes no overall PASS claim.  Current lines require the
    server-normalized marker.  Immutable pre-marker lines require the complete
    redundant server-derived authority shape because source runtime cannot
    independently reconstruct the DB-verified graph tuple used at write time.
    """

    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    test_results = (
        line.get("test_results")
        if isinstance(line.get("test_results"), Mapping)
        else {}
    )
    payload_test_results = (
        payload.get("test_results")
        if isinstance(payload.get("test_results"), Mapping)
        else {}
    )
    verification = (
        line.get("verification")
        if isinstance(line.get("verification"), Mapping)
        else (
            payload.get("verification")
            if isinstance(payload.get("verification"), Mapping)
            else {}
        )
    )
    provenance = (
        line.get("qa_evidence_provenance")
        if isinstance(line.get("qa_evidence_provenance"), Mapping)
        else {}
    )
    binding = (
        provenance.get("authenticated_qa_binding")
        if isinstance(provenance.get("authenticated_qa_binding"), Mapping)
        else {}
    )
    status_gate = (
        provenance.get("completion_status_gate")
        if isinstance(provenance.get("completion_status_gate"), Mapping)
        else {}
    )
    artifact_refs = (
        line.get("artifact_refs")
        if isinstance(line.get("artifact_refs"), Mapping)
        else {}
    )
    ledger = (
        artifact_refs.get("external_no_pass_baseline_ledger")
        if isinstance(
            artifact_refs.get("external_no_pass_baseline_ledger"), Mapping
        )
        else {}
    )
    if not (
        str(line.get("actor_role") or "").strip() == "qa"
        and str(line.get("evidence_kind") or "").strip()
        == "independent_verification"
        and str(line.get("status") or "").strip().lower() == "accepted"
        and str(line.get("authorization_source") or "")
        == "qa_session_token_ref"
        and line.get("observer_impersonation") is False
        and line.get("parent_materialization_authorized") is False
        and str(provenance.get("schema_version") or "")
        == "qa_evidence_provenance.v1"
        and provenance.get("server_derived") is True
        and str(provenance.get("authorization_source") or "")
        == "qa_session_token_ref"
        and str(provenance.get("evidence_owner_role") or "") == "qa"
        and provenance.get("observer_impersonation") is False
        and provenance.get("parent_materialization_authorized") is False
        and str(binding.get("schema_version") or "")
        == "contract_runtime.authenticated_qa_binding.v1"
        and binding.get("server_derived") is True
        and binding.get("independent_verification_session_matched") is True
        and str(binding.get("qa_principal") or "").strip()
        and str(binding.get("qa_session_id") or "").strip()
        and str(status_gate.get("schema_version") or "")
        == _QA_COMPLETION_STATUS_GATE_SCHEMA_VERSION
        and status_gate.get("server_derived") is True
        and status_gate.get("top_level_status_present") is True
        and status_gate.get("top_level_status_passing") is True
        and str(status_gate.get("normalized_status") or "") == "accepted"
        and status_gate.get("nested_payload_decision_satisfies") is False
        and str(payload.get("schema_version") or "")
        in _QA_NO_PASS_PAYLOAD_SCHEMA_VERSIONS
        and (
            str(payload.get("acceptance_scope") or "")
            == "candidate_regression_and_acceptance_criteria"
            or payload.get("row_scoped_qa_pass") is True
        )
        and str(payload.get("verdict") or "").strip().lower() == "accepted"
        and str(payload.get("full_suite_claim") or "") == "not_claimed"
        and payload.get("candidate_new_failures") == 0
        and not list(payload.get("candidate_specific_issues") or [])
        and payload.get("no_pass_claim") is True
        and payload.get("overall_release_pass_claimed") is False
        and test_results.get("candidate_new_failures") == 0
        and not list(test_results.get("candidate_specific_issues") or [])
        and (
            test_results.get("no_pass_claim") is True
            or test_results.get("no_pass") is True
        )
        and test_results.get("overall_release_pass_claimed") is False
        and verification.get(
            "candidate_new_failures",
            verification.get("candidate_specific_new_failures"),
        )
        == 0
        and not list(verification.get("candidate_specific_issues") or [])
        and verification.get("no_pass_claim") is True
        and verification.get("overall_release_pass_claimed") is False
        and str(verification.get("verdict") or "").strip().lower()
        == "accepted"
        and str(ledger.get("schema_version") or "")
        == _QA_NO_PASS_LEDGER_SCHEMA_VERSION
        and ledger.get("candidate_new_failures") == 0
        and not list(ledger.get("candidate_specific_issues") or [])
        and ledger.get("no_pass_claim") is True
        and ledger.get("overall_release_pass_claimed") is False
        and list(ledger.get("refs") or [])
    ):
        return False

    base_commit = str(ledger.get("base_commit_sha") or "").strip().lower()
    candidate_commit = str(
        ledger.get("candidate_commit_sha") or ""
    ).strip().lower()
    line_commit = str(line.get("commit_sha") or "").strip().lower()
    payload_base_commit = str(payload.get("base_commit_sha") or "").strip().lower()
    payload_candidate_commit = str(
        payload.get("candidate_commit_sha") or ""
    ).strip().lower()
    if not (
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit)
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", candidate_commit)
        and line_commit == candidate_commit
    ):
        return False
    # The canonical v2 ledger is written by the server from the DB-verified QA
    # graph tuple.  Payload copies of that tuple are compatibility fields, not
    # a second authority source: current accepted lines may omit either copy.
    # Preserve fail-closed handling for contradictory copies when present.
    if (
        ("base_commit_sha" in payload and payload_base_commit != base_commit)
        or (
            "candidate_commit_sha" in payload
            and payload_candidate_commit != candidate_commit
        )
    ):
        return False
    # Marker-absent compatibility remains pinned to the immutable historical
    # line and its complete redundant server-derived authority shape.
    if ledger.get("server_normalized") is not True and not (
        payload_base_commit == base_commit
        and payload_candidate_commit == candidate_commit
    ):
        return False

    base_failures = _qa_no_pass_failure_identities(
        ledger.get("base_failure_identities")
    )
    candidate_failures = _qa_no_pass_failure_identities(
        ledger.get("candidate_failure_identities")
    )
    base_reproduction = (
        ledger.get("base_reproduction")
        if isinstance(ledger.get("base_reproduction"), Mapping)
        else {}
    )
    reproduced_failures = _qa_no_pass_failure_identities(
        base_reproduction.get("failure_identities")
    )
    candidate_counts = (
        ledger.get("candidate_suite_counts")
        if isinstance(ledger.get("candidate_suite_counts"), Mapping)
        else {}
    )
    reproduced = base_reproduction.get("reproduced")
    total = base_reproduction.get("total")
    baseline_known = candidate_counts.get("baseline_known_non_green")
    failed = candidate_counts.get("failed")
    passed = candidate_counts.get("passed")
    if not (
        base_failures
        and base_failures == candidate_failures == reproduced_failures
        and _qa_no_pass_exact_positive_count(reproduced, len(base_failures))
        and _qa_no_pass_exact_positive_count(total, len(base_failures))
        and _qa_no_pass_exact_positive_count(baseline_known, len(base_failures))
        and _qa_no_pass_exact_positive_count(failed, len(base_failures))
        and isinstance(passed, int)
        and not isinstance(passed, bool)
        and passed > 0
    ):
        return False

    if not _qa_no_pass_ledger_marker_allows_completion(
        line,
        payload=payload,
        test_results=test_results,
        payload_test_results=payload_test_results,
        verification=verification,
        provenance=provenance,
        binding=binding,
        status_gate=status_gate,
        ledger=ledger,
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        base_failures=base_failures,
        candidate_failures=candidate_failures,
        passed=passed,
        source_record=source_record,
        source_line_index=source_line_index,
    ):
        return False

    for result_source in (test_results, payload_test_results):
        if result_source and not _qa_no_pass_result_counts_match_ledger(
            result_source,
            base_failures=base_failures,
            candidate_failures=candidate_failures,
        ):
            return False
    for result_source in (
        line,
        payload,
        test_results,
        payload_test_results,
        verification,
        ledger,
    ):
        if _qa_no_pass_source_claims_pass(result_source):
            return False
    if any(
        _qa_independent_verification_summary_reports_failure(source)
        for source in (payload, test_results, verification)
    ):
        return False
    return True


def _qa_no_pass_ledger_marker_allows_completion(
    line: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    test_results: Mapping[str, Any],
    payload_test_results: Mapping[str, Any],
    verification: Mapping[str, Any],
    provenance: Mapping[str, Any],
    binding: Mapping[str, Any],
    status_gate: Mapping[str, Any],
    ledger: Mapping[str, Any],
    base_commit: str,
    candidate_commit: str,
    base_failures: tuple[str, ...],
    candidate_failures: tuple[str, ...],
    passed: int,
    source_record: Mapping[str, Any] | None,
    source_line_index: int,
) -> bool:
    """Accept current normalization or the immutable pre-marker line shape.

    The marker-absent branch is read-only compatibility for persisted lines
    written by the server before the ledger marker could be stored.  It binds
    the duplicated commit tuple, result ledger, authenticated QA principal,
    and status gate instead of treating marker absence as caller authority.
    An explicit false marker is never legacy evidence.
    """

    if ledger.get("server_normalized") is True:
        return True
    if "server_normalized" in ledger:
        return False
    if not _qa_no_pass_persisted_legacy_source_context(
        line,
        source_record=source_record,
        source_line_index=source_line_index,
        base_commit=base_commit,
        candidate_commit=candidate_commit,
    ):
        return False

    principal = str(binding.get("qa_principal") or "").strip()
    session_id = str(binding.get("qa_session_id") or "").strip()
    if not (
        str(provenance.get("source") or "")
        == "contract_runtime_qa_independent_verification_binding"
        and str(status_gate.get("source") or "")
        == "contract_runtime_line_write_normalization"
        and principal
        and session_id
        and all(
            str(source.get(field) or "").strip() == principal
            for source, field in (
                (line, "actor_session_principal"),
                (line, "evidence_owner_actor"),
                (line, "submitter_principal"),
                (provenance, "evidence_owner_actor"),
                (provenance, "submitter_principal"),
            )
        )
        and all(
            str(source.get(field) or "").strip() == session_id
            for source, field in (
                (line, "evidence_owner_session"),
                (line, "submitter_session"),
                (provenance, "evidence_owner_session"),
                (provenance, "submitter_session"),
            )
        )
        and all(
            str(line.get(field) or "").strip().lower() == candidate_commit
            for field in (
                "commit_sha",
                "immutable_head_commit",
                "validated_head_commit",
            )
        )
        and verification.get("exact_commit_tuple_verified") is True
    ):
        return False

    payload_verification = (
        payload.get("verification")
        if isinstance(payload.get("verification"), Mapping)
        else {}
    )
    payload_artifact_refs = (
        payload.get("artifact_refs")
        if isinstance(payload.get("artifact_refs"), Mapping)
        else {}
    )
    payload_ledger = (
        payload_artifact_refs.get("external_no_pass_baseline_ledger")
        if isinstance(
            payload_artifact_refs.get("external_no_pass_baseline_ledger"),
            Mapping,
        )
        else {}
    )
    commit_sources = (
        (payload, "base_commit_sha"),
        (test_results, "baseline_commit_sha"),
        (payload_test_results, "baseline_commit_sha"),
        (payload_ledger, "base_commit_sha"),
    )
    if not (
        payload_verification.get("exact_commit_tuple_verified") is True
        and all(
            str(source.get(base_field) or "").strip().lower()
            == base_commit
            and str(source.get("candidate_commit_sha") or "").strip().lower()
            == candidate_commit
            for source, base_field in commit_sources
        )
        and str(payload_ledger.get("schema_version") or "")
        == _QA_NO_PASS_LEDGER_SCHEMA_VERSION
        and "server_normalized" not in payload_ledger
        and list(line.get("graph_trace_ids") or [])
        and list(
            (
                line.get("artifact_refs")
                if isinstance(line.get("artifact_refs"), Mapping)
                else {}
            ).get("durable_authority_refs")
            or []
        )
    ):
        return False

    payload_base_reproduction = (
        payload_ledger.get("base_reproduction")
        if isinstance(payload_ledger.get("base_reproduction"), Mapping)
        else {}
    )
    payload_candidate_counts = (
        payload_ledger.get("candidate_suite_counts")
        if isinstance(payload_ledger.get("candidate_suite_counts"), Mapping)
        else {}
    )
    expected_count = len(base_failures)
    return bool(
        base_failures == candidate_failures
        and _qa_no_pass_failure_identities(
            test_results.get("baseline_failure_node_ids")
        )
        == base_failures
        and _qa_no_pass_failure_identities(
            test_results.get("candidate_failure_node_ids")
        )
        == candidate_failures
        and _qa_no_pass_failure_identities(
            payload_test_results.get("baseline_failure_node_ids")
        )
        == base_failures
        and _qa_no_pass_failure_identities(
            payload_test_results.get("candidate_failure_node_ids")
        )
        == candidate_failures
        and _qa_no_pass_failure_identities(
            payload_ledger.get("base_failure_identities")
        )
        == base_failures
        and _qa_no_pass_failure_identities(
            payload_ledger.get("candidate_failure_identities")
        )
        == candidate_failures
        and _qa_no_pass_failure_identities(
            payload_base_reproduction.get("failure_identities")
        )
        == base_failures
        and all(
            _qa_no_pass_exact_positive_count(source.get(field), expected_count)
            for source, field in (
                (test_results, "baseline_failed"),
                (test_results, "candidate_failed"),
                (payload_test_results, "baseline_failed"),
                (payload_test_results, "candidate_failed"),
                (payload_base_reproduction, "reproduced"),
                (payload_base_reproduction, "total"),
                (payload_candidate_counts, "baseline_known_non_green"),
                (payload_candidate_counts, "failed"),
            )
        )
        and test_results.get("baseline_passed") == passed
        and test_results.get("candidate_passed") == passed
        and payload_test_results.get("baseline_passed") == passed
        and payload_test_results.get("candidate_passed") == passed
        and payload_candidate_counts.get("passed") == passed
        and payload_ledger.get("candidate_new_failures") == 0
        and not list(payload_ledger.get("candidate_specific_issues") or [])
        and payload_ledger.get("no_pass_claim") is True
        and payload_ledger.get("overall_release_pass_claimed") is False
        and list(payload_ledger.get("refs") or [])
    )


def _qa_no_pass_persisted_legacy_source_context(
    line: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any] | None,
    source_line_index: int,
    base_commit: str,
    candidate_commit: str,
) -> bool:
    """Bind pre-marker compatibility to one trusted persisted record member."""

    if not isinstance(source_record, Mapping):
        return False
    persisted_line_index = _source_record_completed_line_index(line, source_record)
    if persisted_line_index < 0 or source_line_index != persisted_line_index:
        return False
    stored_lines = source_record.get("completed_lines")
    if not isinstance(stored_lines, list):
        return False
    persisted_identity = (
        str(source_record.get("contract_execution_id") or "").strip(),
        persisted_line_index,
    )
    if persisted_identity not in _QA_NO_PASS_PRE_MARKER_PERSISTED_LINE_IDENTITIES:
        return False
    if not (
        str(source_record.get("schema_version") or "")
        == "contract_runtime_execution_record.v1"
        and _record_contract_id(source_record) in {"mf_parallel", "mf_parallel.v2"}
        and str(source_record.get("project_id") or "").strip()
        and str(source_record.get("backlog_id") or "").strip()
        and str(source_record.get("contract_execution_id") or "").strip()
    ):
        return False

    payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
    execution_id = str(source_record.get("contract_execution_id") or "").strip()
    payload_execution_id = str(payload.get("contract_execution_id") or "").strip()
    if payload_execution_id and payload_execution_id != execution_id:
        return False
    line_trace_ids = _qa_no_pass_failure_identities(line.get("graph_trace_ids"))
    if not line_trace_ids:
        return False

    provenance = (
        line.get("qa_evidence_provenance")
        if isinstance(line.get("qa_evidence_provenance"), Mapping)
        else {}
    )
    binding = (
        provenance.get("authenticated_qa_binding")
        if isinstance(provenance.get("authenticated_qa_binding"), Mapping)
        else {}
    )
    principal = str(binding.get("qa_principal") or "").strip()
    session_id = str(binding.get("qa_session_id") or "").strip()

    for graph_line in reversed(stored_lines[:persisted_line_index]):
        if not isinstance(graph_line, Mapping):
            continue
        if str(graph_line.get("line_id") or "").strip() != "qa_graph_context":
            continue
        graph_payload = (
            graph_line.get("payload")
            if isinstance(graph_line.get("payload"), Mapping)
            else {}
        )
        graph_evidence = (
            graph_payload.get("graph_trace_evidence")
            if isinstance(graph_payload.get("graph_trace_evidence"), Mapping)
            else {}
        )
        graph_provenance = (
            graph_line.get("qa_evidence_provenance")
            if isinstance(graph_line.get("qa_evidence_provenance"), Mapping)
            else {}
        )
        graph_binding = (
            graph_provenance.get("authenticated_qa_binding")
            if isinstance(
                graph_provenance.get("authenticated_qa_binding"), Mapping
            )
            else {}
        )
        graph_trace_ids = _qa_no_pass_failure_identities(
            graph_evidence.get("verified_trace_ids")
            or graph_evidence.get("trace_ids")
        )
        graph_status = str(
            graph_line.get("status") or graph_payload.get("status") or ""
        ).strip().lower()
        if (
            str(graph_line.get("actor_role") or "").strip() == "qa"
            and graph_status in {"", "accepted"}
            and str(graph_line.get("authorization_source") or "")
            == "qa_session_token_ref"
            and graph_line.get("observer_impersonation") is False
            and graph_line.get("parent_materialization_authorized") is False
            and str(graph_provenance.get("schema_version") or "")
            == "qa_evidence_provenance.v1"
            and str(graph_provenance.get("source") or "")
            == "contract_runtime_qa_graph_authority_binding"
            and graph_provenance.get("server_derived") is True
            and str(graph_binding.get("schema_version") or "")
            == "contract_runtime.authenticated_qa_binding.v1"
            and graph_binding.get("server_derived") is True
            and graph_binding.get("graph_trace_session_matched") is True
            and str(graph_binding.get("qa_principal") or "").strip() == principal
            and str(graph_binding.get("qa_session_id") or "").strip() == session_id
            and str(graph_line.get("commit_sha") or "").strip().lower()
            == candidate_commit
            and str(graph_evidence.get("base_commit_sha") or "").strip().lower()
            == base_commit
            and str(
                graph_evidence.get("candidate_commit_sha")
                or graph_evidence.get("candidate_commit")
                or ""
            )
            .strip()
            .lower()
            == candidate_commit
            and graph_evidence.get("db_verified") is True
            and not list(graph_evidence.get("missing_trace_ids") or [])
            and not list(graph_evidence.get("identity_mismatches") or [])
            and graph_trace_ids
            and set(line_trace_ids).issubset(set(graph_trace_ids))
            and str(graph_evidence.get("project_id") or "").strip()
            == str(source_record.get("project_id") or "").strip()
            and str(graph_evidence.get("backlog_id") or "").strip()
            == str(source_record.get("backlog_id") or "").strip()
        ):
            return True
    return False


def _qa_no_pass_failure_identities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    identities = tuple(str(item or "").strip() for item in value)
    if not identities or any(not item for item in identities):
        return ()
    if len(set(identities)) != len(identities):
        return ()
    return tuple(sorted(identities))


def _qa_no_pass_exact_positive_count(value: Any, expected: int) -> bool:
    return bool(
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        and value == expected
    )


def _qa_no_pass_result_counts_match_ledger(
    value: Mapping[str, Any],
    *,
    base_failures: tuple[str, ...],
    candidate_failures: tuple[str, ...],
) -> bool:
    expected = len(base_failures)
    for field, identities in (
        ("baseline_failure_node_ids", base_failures),
        ("base_failure_identities", base_failures),
        ("candidate_failure_node_ids", candidate_failures),
        ("candidate_failure_identities", candidate_failures),
    ):
        if field in value and _qa_no_pass_failure_identities(value.get(field)) != identities:
            return False
    for field in ("baseline_failed", "candidate_failed", "failed"):
        if field in value and not _qa_no_pass_exact_positive_count(
            value.get(field), expected
        ):
            return False
    for branch_name, identities in (
        ("base", base_failures),
        ("baseline", base_failures),
        ("candidate", candidate_failures),
    ):
        branch = value.get(branch_name)
        if not isinstance(branch, Mapping):
            continue
        if "failed" in branch and not _qa_no_pass_exact_positive_count(
            branch.get("failed"), len(identities)
        ):
            return False
        for field in ("failure_identities", "failure_node_ids"):
            if field in branch and _qa_no_pass_failure_identities(
                branch.get(field)
            ) != identities:
                return False
    return True


def _qa_no_pass_source_claims_pass(value: Mapping[str, Any]) -> bool:
    if any(
        value.get(field) is True
        for field in (
            "passed",
            "overall_release_pass",
            "overall_release_pass_claimed",
            "full_suite_passed",
        )
    ):
        return True
    for field in ("base", "baseline", "candidate"):
        nested = value.get(field)
        if isinstance(nested, Mapping) and nested.get("passed") is True:
            return True
    return False


_QA_FAILURE_SUMMARY_FIELDS = frozenset(
    {
        "summary",
        "tests_summary",
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
    projection = {
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
    same_row_recovery_cursor = (
        next_legal_action.get("same_row_recovery_cursor")
        if isinstance(next_legal_action, Mapping)
        and isinstance(
            next_legal_action.get("same_row_recovery_cursor"), Mapping
        )
        else {}
    )
    if same_row_recovery_cursor:
        projection["same_row_recovery_cursor"] = dict(
            same_row_recovery_cursor
        )
    completed_repair_barrier = (
        active_chain.get("completed_repair_fresh_generation_barrier")
        if isinstance(
            active_chain.get("completed_repair_fresh_generation_barrier"),
            Mapping,
        )
        else {}
    )
    if completed_repair_barrier:
        projection.update(
            {
                "completed_repair_fresh_generation_barrier": dict(
                    completed_repair_barrier
                ),
                "scheduler_eligible": False,
                "schedulable": False,
                "resume_eligible": False,
                "resumable": False,
                "next_legal_action": {},
            }
        )
        projection.pop("parent_to_resume_contract_execution_id", None)
    if projection["readiness_state"] == "terminal_retired":
        terminal_retirement = (
            active_chain.get("terminal_retirement")
            if isinstance(active_chain.get("terminal_retirement"), Mapping)
            else {}
        )
        historical_pinned_identity = (
            active_chain.get("historical_pinned_identity")
            if isinstance(
                active_chain.get("historical_pinned_identity"), Mapping
            )
            else {}
        )
        projection.update(
            {
                "disposition": "terminal_retired",
                "terminal": True,
                "historical_pinned_read_only": True,
                "scheduler_eligible": False,
                "schedulable": False,
                "current_eligible": False,
                "close_eligible": False,
                "closeable": False,
                "resume_eligible": False,
                "resumable": False,
                "retry_eligible": False,
                "write_eligible": False,
                "next_legal_action": {},
                "terminal_retirement": dict(terminal_retirement),
                "historical_pinned_identity": dict(
                    historical_pinned_identity
                ),
            }
        )
        projection.pop("parent_to_resume_contract_execution_id", None)
    if projection["readiness_state"] == "completed_with_exception":
        stored_terminal = (
            active_chain.get("terminal_disposition")
            if isinstance(active_chain.get("terminal_disposition"), Mapping)
            else {}
        )
        projection.update(
            {
                "row_status": "WAIVED",
                "source_row_status": "WAIVED",
                "disposition": "completed_with_exception",
                "terminal": True,
                "scheduler_eligible": False,
                "schedulable": False,
                "current_eligible": False,
                "close_eligible": False,
                "closeable": False,
                "resume_eligible": False,
                "resumable": False,
                "terminal_disposition": (
                    dict(stored_terminal)
                    if stored_terminal
                    else {
                        "schema_version": (
                            "contract_runtime.audited_bypass_terminal.v1"
                        ),
                        "status": "WAIVED",
                        "readiness_state": "completed_with_exception",
                        "source": "backlog_contract_chain_current",
                        "no_pass_claim": True,
                    }
                ),
            }
        )
        recovery_fallback = active_chain.get("bypass_recovery_fallback")
        if isinstance(recovery_fallback, Mapping) and recovery_fallback:
            projection["bypass_recovery_fallback"] = dict(
                recovery_fallback
            )
        projection.pop("parent_to_resume_contract_execution_id", None)
    return projection


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
    cache_key = (project_id, task_id)
    use_cache = fetcher is None
    if use_cache:
        cached = _JUDGMENT_HINT_CACHE.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < _JUDGMENT_HINT_CACHE_TTL_SECONDS:
            return deepcopy(cached[1])
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
        if use_cache:
            _JUDGMENT_HINT_CACHE[cache_key] = (time.monotonic(), None)
        return None
    if not hints:
        log.info("judgment_hints_gap: empty or missing hints")
        if use_cache:
            _JUDGMENT_HINT_CACHE[cache_key] = (time.monotonic(), None)
        return None
    if use_cache:
        _JUDGMENT_HINT_CACHE[cache_key] = (time.monotonic(), deepcopy(hints))
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
        definition = self.registry.resolve_for_new_execution(
            contract_id,
            version=version,
            requested_revision=revision,
        )
        common_rule_join = self.registry.resolve_common_rule_applicability(
            definition
        )
        if not is_new_execution_allowed(definition):
            lifecycle = (definition.get("metadata") or {}).get("lifecycle") or {}
            if (
                isinstance(lifecycle, Mapping)
                and lifecycle.get("terminal_retirement") is True
            ):
                raise ContractRetirementError(definition)
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
                "authoritative_common_rule_join": common_rule_join,
            },
        )
        if start_precheck.decision == "block" and _enforce_start_precheck(definition):
            raise ContractRuntimeError("; ".join(start_precheck.errors))
        if _guide_bound_server_projection_only(definition):
            raise ContractRuntimeError(
                "guide_bound_server_projected_batch_parent: generic "
                "ContractRuntime execution is not the Batch runtime path"
            )
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
        _bind_authoritative_common_rule_join_to_state(state, common_rule_join)
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
            judgment_hints=judgment_hints,
        )
        _bind_authoritative_common_rule_join_to_guide(
            guide,
            common_rule_join,
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
            "authoritative_common_rule_join": deepcopy(common_rule_join),
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
        """Refresh the legacy stored guide; live read facades use ``current_record``."""

        view = self.current_record(
            contract_execution_id,
            actor_role=actor_role,
        )
        if self._terminal_retirement_for_record(view) is None:
            self.store.update(contract_execution_id, view)
        return dict(view["runtime_guide"])

    def current_record(
        self,
        contract_execution_id: str,
        *,
        actor_role: str | None = None,
    ) -> dict[str, Any]:
        """Return the current derived execution view without taking a write lock."""

        record = self.store.get(contract_execution_id)
        return self._record_view(
            record,
            actor_role=actor_role,
            completed_lines=record.get("completed_lines") or [],
        )

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

    def mf_parallel_atomic_lane_gate_view(
        self,
        record: Mapping[str, Any],
        runtime_guide: Mapping[str, Any],
        write: Mapping[str, Any],
        *,
        source_record: Mapping[str, Any] | None = None,
        projection: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the server-derived gate view for one atomic worker lane.

        This does not mutate the persisted/global scheduler view.  Facades use
        it only to construct the exact writer-bound body that
        :meth:`submit_line_write` will independently re-derive and validate.
        """

        authoritative_source = source_record or record
        definition = self._load_pinned_definition(authoritative_source)
        completion_satisfying_lines = (
            _contract_completion_satisfying_lines_for_view(
                definition,
                authoritative_source,
                [
                    item
                    for item in (record.get("completed_lines") or [])
                    if isinstance(item, Mapping)
                ],
                projection,
            )
        )
        return _mf_parallel_atomic_lane_gate_view(
            definition,
            record,
            runtime_guide,
            write,
            completion_satisfying_lines=completion_satisfying_lines,
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
        common_rule_join = self._authoritative_common_rule_join(
            record,
            definition,
        )
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
        completion_satisfying_lines = (
            _contract_completion_satisfying_lines_for_view(
                definition,
                record,
                line_items,
                projection,
            )
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
        _bind_authoritative_common_rule_join_to_state(state, common_rule_join)
        guide = compile_runtime_guide(
            definition,
            state,
            instruction_bundle=instruction_bundle,
            judgment_hints=_record_judgment_hints(record),
        )
        _bind_authoritative_common_rule_join_to_guide(
            guide,
            common_rule_join,
        )
        terminal_disposition = _audited_bypass_terminal_disposition(
            {**dict(record), "completed_lines": line_items}
        )
        pinned_terminal_no_pass = _pinned_terminal_no_pass_disposition(
            definition,
            line_items,
        )
        if terminal_disposition:
            guide["next_legal_action"] = None
            guide["terminal_disposition"] = terminal_disposition
            recovery_fallback = terminal_disposition.get(
                "bypass_recovery_fallback"
            )
            if isinstance(recovery_fallback, Mapping) and recovery_fallback:
                guide["bypass_recovery_fallback"] = dict(
                    recovery_fallback
                )
            guide["readiness_state"] = "completed_with_exception"
            guide["disposition"] = "completed_with_exception"
            state["readiness_state"] = "completed_with_exception"
            state["disposition"] = "completed_with_exception"
            state["terminal"] = True
            state["scheduler_eligible"] = False
            state["current_eligible"] = False
            state["close_eligible"] = False
            state["resume_eligible"] = False
            if isinstance(recovery_fallback, Mapping) and recovery_fallback:
                state["bypass_recovery_fallback"] = dict(
                    recovery_fallback
                )
        if not pinned_terminal_no_pass:
            _attach_failed_qa_rework_guidance(
                guide,
                line_items=line_items,
                source_record=record,
            )
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
        _attach_line_bypass_guidance(
            guide,
            record=record,
            reader_role=effective_actor_role,
        )
        if pinned_terminal_no_pass:
            guide["next_legal_action"] = None
            guide.pop("writer_role_safe_copy_payload", None)
            guide.pop("line_bypass_guidance", None)
            guide.pop("failed_qa_rework", None)
            guide.pop("post_projection_submit_line_guidance", None)
            guide["terminal_disposition"] = dict(
                pinned_terminal_no_pass
            )
            guide["readiness_state"] = "terminal_no_pass"
            guide["disposition"] = "terminal_no_pass"
            guide["scheduler_eligible"] = False
            guide["resume_eligible"] = False
            guide["retry_eligible"] = False
            guide["write_eligible"] = False
            state["readiness_state"] = "terminal_no_pass"
            state["disposition"] = "terminal_no_pass"
            state["terminal"] = True
            state["scheduler_eligible"] = False
            state["current_eligible"] = False
            state["close_eligible"] = False
            state["resume_eligible"] = False
            state["retry_eligible"] = False
            state["write_eligible"] = False
            guide["runtime_guide_hash"] = stable_sha256(
                {
                    key: value
                    for key, value in guide.items()
                    if key != "runtime_guide_hash"
                }
            )
        retirement = self._terminal_retirement_for_record(record)
        if retirement is not None:
            retirement_result = ContractRetirementError(retirement).to_dict()
            guide["next_legal_action"] = None
            guide.pop("writer_role_safe_copy_payload", None)
            guide.pop("line_bypass_guidance", None)
            guide.pop("failed_qa_rework", None)
            guide.pop("post_projection_submit_line_guidance", None)
            guide["readiness_state"] = "terminal_retired"
            guide["disposition"] = "terminal_retired"
            guide["terminal_retirement"] = retirement_result
            guide["historical_pinned_read_only"] = True
            guide["scheduler_eligible"] = False
            guide["resume_eligible"] = False
            guide["retry_eligible"] = False
            guide["write_eligible"] = False
            state["readiness_state"] = "terminal_retired"
            state["disposition"] = "terminal_retired"
            state["terminal"] = True
            state["scheduler_eligible"] = False
            state["current_eligible"] = False
            state["close_eligible"] = False
            state["resume_eligible"] = False
            state["retry_eligible"] = False
            state["write_eligible"] = False
            guide["runtime_guide_hash"] = stable_sha256(
                {
                    key: value
                    for key, value in guide.items()
                    if key != "runtime_guide_hash"
                }
            )
        state["execution_state_hash"] = stable_sha256(
            {
                key: value
                for key, value in state.items()
                if key != "execution_state_hash"
            }
        )
        if retirement is not None:
            retirement_result = ContractRetirementError(retirement).to_dict()
            current_precheck = make_gate_decision(
                action="current_state",
                gate_id="contract_terminal_retirement",
                gate_type="contract_lifecycle",
                actor_role=effective_actor_role,
                errors=[str(retirement_result.get("code") or "contract_retired")],
                next_move=retirement_result.get("next_legal_action") or {},
                policy_hash=stable_sha256(
                    ((retirement.get("metadata") or {}).get("lifecycle") or {})
                ),
                contract_definition_hash=str(
                    retirement.get("definition_hash") or ""
                ),
                execution_state_revision=int(
                    record.get("execution_state_revision") or 0
                ),
                runtime_guide_hash=str(guide.get("runtime_guide_hash") or ""),
            )
        else:
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
                    "authoritative_common_rule_join": common_rule_join,
                },
            )
        _attach_precheck_decision(guide, current_precheck.to_dict())
        view = deepcopy(dict(record))
        view["completed_lines"] = deepcopy(line_items)
        view["execution_state"] = state
        view["runtime_guide"] = guide
        view["precheck_decision"] = current_precheck.to_dict()
        view["authoritative_common_rule_join"] = deepcopy(common_rule_join)
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
        self._raise_if_terminally_retired(record)
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
        refreshed = record
        gate_record = self._record_view(
            refreshed,
            actor_role=effective_actor_role,
            completed_lines=refreshed.get("completed_lines") or [],
        )
        guide = gate_record["runtime_guide"]
        use_completed_line_projection = (
            projected_completed_lines is not None
            and str(effective_write.get("line_id") or "").strip()
            != "worker_commit"
        )
        if use_completed_line_projection:
            gate_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_completed_lines,
                projection=projection,
            )
            guide = gate_record["runtime_guide"]
        gate_state, gate_guide = self.mf_parallel_atomic_lane_gate_view(
            gate_record,
            guide,
            effective_write,
            source_record=refreshed,
            projection=projection,
        )
        # Compiled execution state intentionally omits record metadata. Supply
        # it only to the authoritative write Gate so revision-specific Fact
        # conformance also covers direct Runtime callers without publishing
        # metadata as generic runtime state.
        gate_state = {
            **dict(gate_state),
            "metadata": deepcopy(dict(refreshed.get("metadata") or {})),
            # The rev10 dispatch Gate must prove custody against the admitted
            # prefill Fact, not merely against its metadata projection.  Keep
            # full line evidence internal to the Gate; it is still omitted
            # from the generic compiled-state/public response surfaces.
            "completed_lines": deepcopy(
                list(refreshed.get("completed_lines") or [])
            ),
        }
        _bind_mf_parallel_atomic_lane_runtime_guide_hash(
            effective_write,
            gate_guide,
        )
        gate_decision = self.gate_kernel.precheck(
            definition,
            action="submit_line",
            actor_role=effective_actor_role,
            execution_state=gate_state,
            runtime_guide=gate_guide,
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
                refreshed,
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
        refreshed = self._record_view(
            refreshed,
            actor_role=effective_actor_role,
            completed_lines=completed_lines,
        )
        result_record = deepcopy(refreshed)
        if use_completed_line_projection:
            projected_after_write = list(projected_completed_lines)
            projected_after_write.append(written_line)
            result_record = self._record_view(
                refreshed,
                actor_role=effective_actor_role,
                completed_lines=projected_after_write,
                projection=projection,
            )
        implementation_postcondition_errors = (
            _worker_implementation_atomic_advance_errors(
                result_record,
                written_line,
            )
        )
        if implementation_postcondition_errors:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=tuple(implementation_postcondition_errors),
                ).to_dict(),
                "record": gate_record,
                "zero_contract_runtime_write": True,
                "worker_implementation_atomic_advance": False,
            }
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
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "decision": _gate_decision_payload(gate_decision),
            "record": result_record,
        }

    def revise_failed_qa_worker_implementation(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
    ) -> dict[str, Any]:
        """Append a server-verified canonical implementation revision.

        This is deliberately narrower than a generic duplicate-line or rewind
        facility. It only versions the active ``worker_implementation`` after
        failed independent QA, before the retry ``worker_commit``, for the same
        authenticated worker/runtime identity. Historical implementation and
        QA lines remain append-only; the latest matching implementation becomes
        the worker-commit lineage authority.
        """

        effective_write = dict(write)
        effective_actor_role = _effective_actor_role(
            effective_write,
            actor_role=actor_role,
        )
        if effective_actor_role:
            effective_write["actor_role"] = effective_actor_role
        _enrich_line_instance_fields(effective_write)

        record = self.store.get(contract_execution_id)
        self._raise_if_terminally_retired(record)
        lines = list(record.get("completed_lines") or [])
        failed_qa_index = _active_failed_qa_line_index(
            lines,
            source_record=record,
        )
        payload = (
            dict(effective_write.get("payload"))
            if isinstance(effective_write.get("payload"), Mapping)
            else {}
        )
        authority = (
            dict(payload.get("canonical_rework_lineage_revision_authority"))
            if isinstance(
                payload.get("canonical_rework_lineage_revision_authority"),
                Mapping,
            )
            else {}
        )
        rejoin_marker = (
            dict(payload.get("failed_qa_revision_rejoin_marker"))
            if isinstance(payload.get("failed_qa_revision_rejoin_marker"), Mapping)
            else {}
        )
        graph_evidence = (
            dict(payload.get("graph_trace_db_evidence"))
            if isinstance(payload.get("graph_trace_db_evidence"), Mapping)
            else {}
        )

        guide = self._record_view(
            record,
            actor_role=effective_actor_role,
            completed_lines=lines,
        )["runtime_guide"]
        next_action = (
            guide.get("next_legal_action")
            if isinstance(guide.get("next_legal_action"), Mapping)
            else {}
        )
        errors: list[str] = []
        if _record_contract_id(record) not in {"mf_parallel", "mf_parallel.v2"}:
            errors.append("implementation revision requires mf_parallel.v2")
        if effective_actor_role != "mf_sub":
            errors.append("implementation revision requires actor_role=mf_sub")
        if str(effective_write.get("line_id") or "").strip() != "worker_implementation":
            errors.append("implementation revision requires worker_implementation")
        if str(effective_write.get("evidence_kind") or "").strip() != "implementation":
            errors.append("implementation revision requires implementation evidence")
        if failed_qa_index < 0:
            errors.append("implementation revision requires active failed independent QA")
        timing_errors: list[str] = []
        if str(next_action.get("line_id") or "").strip() != "worker_commit":
            timing_errors.append(
                "implementation revision is only legal before retry worker_commit"
            )

        runtime_context_id = _worker_commit_text(
            effective_write,
            "runtime_context_id",
        )
        task_id = _worker_commit_text(effective_write, "task_id")
        prior_implementation: Mapping[str, Any] | None = None
        prior_index = -1
        for index in range(len(lines) - 1, failed_qa_index, -1):
            candidate = lines[index]
            if not isinstance(candidate, Mapping):
                continue
            if str(candidate.get("line_id") or "").strip() != "worker_implementation":
                continue
            if runtime_context_id and _worker_commit_text(
                candidate,
                "runtime_context_id",
            ) != runtime_context_id:
                continue
            if task_id and _worker_commit_text(candidate, "task_id") != task_id:
                continue
            prior_implementation = candidate
            prior_index = index
            break
        if prior_implementation is None:
            errors.append("implementation revision requires a post-failed-QA implementation")

        retry_worker_commit_exists = False
        for candidate in lines[failed_qa_index + 1 :]:
            if not isinstance(candidate, Mapping):
                continue
            if str(candidate.get("line_id") or "").strip() != "worker_commit":
                continue
            if runtime_context_id and _worker_commit_text(
                candidate,
                "runtime_context_id",
            ) != runtime_context_id:
                continue
            if task_id and _worker_commit_text(candidate, "task_id") != task_id:
                continue
            retry_worker_commit_exists = True
            break
        if retry_worker_commit_exists:
            timing_errors.append(
                "implementation revision is closed after retry worker_commit"
            )

        identity_fields = (
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "worker_id",
            "worker_slot_id",
            "target_project_root",
            "fence_token_hash",
        )
        if prior_implementation is not None:
            for field in identity_fields:
                prior_value = _worker_commit_text(prior_implementation, field)
                revised_value = _worker_commit_text(effective_write, field)
                if prior_value and revised_value != prior_value:
                    errors.append(
                        f"implementation revision {field} must match prior implementation"
                    )

        prior_session_token_ref = (
            _worker_commit_text(prior_implementation, "session_token_ref")
            if prior_implementation is not None
            else ""
        )
        revised_session_token_ref = _worker_commit_text(
            effective_write,
            "session_token_ref",
        )
        session_token_ref_rotation = (
            dict(rejoin_marker.get("session_token_ref_rotation"))
            if isinstance(
                rejoin_marker.get("session_token_ref_rotation"),
                Mapping,
            )
            else {}
        )
        session_token_ref_rotated = bool(
            prior_session_token_ref
            and revised_session_token_ref != prior_session_token_ref
        )
        if session_token_ref_rotated:
            rotation_errors: list[str] = []
            if not revised_session_token_ref:
                rotation_errors.append("active session_token_ref is required")
            if (
                session_token_ref_rotation.get("server_derived") is not True
                or str(session_token_ref_rotation.get("source") or "").strip()
                != "accepted_runtime_context_rejoin_event"
            ):
                rotation_errors.append("server-derived rejoin authority is required")
            if str(
                session_token_ref_rotation.get("active_session_token_ref") or ""
            ).strip() != revised_session_token_ref:
                rotation_errors.append("active rejoin session_token_ref must match")
            for field in (
                "runtime_context_id",
                "task_id",
                "parent_task_id",
                "worker_id",
                "worker_slot_id",
                "target_project_root",
                "fence_token_hash",
            ):
                expected_value = _worker_commit_text(effective_write, field)
                marker_value = str(
                    session_token_ref_rotation.get(field) or ""
                ).strip()
                if expected_value and marker_value != expected_value:
                    rotation_errors.append(f"rejoin {field} must match")
            marker_contract_execution_id = str(
                session_token_ref_rotation.get("contract_execution_id") or ""
            ).strip()
            if marker_contract_execution_id != contract_execution_id:
                rotation_errors.append("rejoin contract_execution_id must match")
            if str(
                session_token_ref_rotation.get("revision_event_ref") or ""
            ).strip() != str(rejoin_marker.get("revision_event_ref") or "").strip():
                rotation_errors.append("rejoin revision boundary must match")
            if rotation_errors:
                errors.append(
                    "implementation revision session_token_ref must match prior "
                    "implementation or an audited same-worker rejoin: "
                    + "; ".join(rotation_errors)
                )

        commit_sha = str(effective_write.get("commit_sha") or "").strip()
        changed_files = sorted(
            set(_worker_commit_strings(effective_write, "changed_files"))
        )
        graph_trace_ids = sorted(
            set(
                _worker_commit_strings(
                    effective_write,
                    "graph_trace_ids",
                    "graph_query_trace_ids",
                    "verified_trace_ids",
                )
            )
        )
        if not _WORKER_COMMIT_SHA_RE.fullmatch(commit_sha):
            errors.append("implementation revision requires a full immutable commit_sha")
        if not changed_files:
            errors.append("implementation revision requires cumulative changed_files")
        if not graph_trace_ids:
            errors.append("implementation revision requires graph_trace_ids")
        if authority.get("server_derived") is not True or str(
            authority.get("source") or ""
        ) != "runtime_context_clean_cumulative_git_revision":
            errors.append("implementation revision requires server-derived git authority")
        if authority.get("clean_worktree") is not True:
            errors.append("implementation revision requires a clean worktree")
        if str(authority.get("actual_head_commit") or "").strip() != commit_sha:
            errors.append("implementation revision commit must match actual worker HEAD")
        if sorted(set(authority.get("cumulative_changed_files") or [])) != changed_files:
            errors.append("implementation revision files must match cumulative runtime diff")
        owned_files = set(authority.get("owned_files") or [])
        fence_containment = _worker_fence_containment(
            changed_files,
            sorted(owned_files),
            repository_root=str(
                authority.get("fence_repository_root")
                or effective_write.get("target_project_root")
                or payload.get("target_project_root")
                or ""
            ).strip(),
        )
        if not fence_containment["ok"]:
            errors.append("implementation revision files must remain inside the worker fence")
        revision_event_ref = str(rejoin_marker.get("revision_event_ref") or "").strip()
        if not revision_event_ref or str(
            authority.get("revision_event_ref") or ""
        ).strip() != revision_event_ref:
            errors.append("implementation revision requires the accepted failed-QA rejoin boundary")
        if graph_evidence.get("db_verified") is not True or sorted(
            set(graph_evidence.get("verified_trace_ids") or [])
        ) != graph_trace_ids:
            errors.append("implementation revision requires exact DB-verified graph traces")

        prior_payload = (
            prior_implementation.get("payload")
            if prior_implementation is not None
            and isinstance(prior_implementation.get("payload"), Mapping)
            else {}
        )
        prior_revision = (
            prior_payload.get("canonical_rework_lineage_revision")
            if isinstance(
                prior_payload.get("canonical_rework_lineage_revision"),
                Mapping,
            )
            else {}
        )
        exact_replay = bool(
            prior_implementation is not None
            and str(prior_implementation.get("commit_sha") or "").strip()
            == commit_sha
            and sorted(
                set(
                    _worker_commit_strings(
                        prior_implementation,
                        "changed_files",
                    )
                )
            )
            == changed_files
            and sorted(
                set(
                    _worker_commit_strings(
                        prior_implementation,
                        "graph_trace_ids",
                        "graph_query_trace_ids",
                        "verified_trace_ids",
                    )
                )
            )
            == graph_trace_ids
            and _worker_commit_text(
                prior_implementation,
                "session_token_ref",
            )
            == revised_session_token_ref
            and str(prior_revision.get("revision_event_ref") or "").strip()
            == revision_event_ref
        )
        if errors:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=tuple(dict.fromkeys(errors)),
                ).to_dict(),
                "record": record,
            }
        if exact_replay:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": True,
                "status": "already_completed",
                "decision": WriteGateDecision(ok=True).to_dict(),
                "record": record,
                "supersedes_implementation_lineage_ref": str(
                    prior_revision.get(
                        "supersedes_implementation_lineage_ref"
                    )
                    or ""
                ),
            }
        if timing_errors:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=tuple(dict.fromkeys(timing_errors)),
                ).to_dict(),
                "record": record,
            }

        prior_lineage = _worker_implementation_lineage(
            record,
            prior_implementation or {},
        )
        payload["canonical_rework_lineage_revision"] = {
            "schema_version": (
                "contract_runtime.worker_implementation_rework_revision.v1"
            ),
            "source": "server_verified_failed_qa_rework",
            "failed_qa_completed_line_index": failed_qa_index,
            "superseded_completed_line_index": prior_index,
            "supersedes_implementation_lineage_ref": prior_lineage[
                "implementation_lineage_ref"
            ],
            "revision_event_ref": revision_event_ref,
            "commit_sha": commit_sha,
            "append_only_history_preserved": True,
            "session_token_ref_rotation": {
                "applied": session_token_ref_rotated,
                "source": (
                    "accepted_runtime_context_rejoin_event"
                    if session_token_ref_rotated
                    else "same_session_token_ref"
                ),
                "revision_event_ref": revision_event_ref,
                "raw_session_tokens_persisted": False,
            },
        }
        effective_write["payload"] = payload
        written_line = _line_evidence_from_write(
            effective_write,
            effective_actor_role,
        )
        completed_lines = [*lines, written_line]
        expected_revision = int(record.get("execution_state_revision") or 1)
        candidate = dict(record)
        candidate["completed_lines"] = completed_lines
        candidate["execution_state_revision"] = expected_revision + 1
        prepared = self._record_view(
            candidate,
            actor_role=effective_actor_role,
            completed_lines=completed_lines,
        )
        prepared_next_action = (
            prepared.get("runtime_guide", {}).get("next_legal_action", {})
            if isinstance(prepared.get("runtime_guide"), Mapping)
            else {}
        )
        if (
            str(prepared_next_action.get("line_id") or "").strip()
            != "worker_commit"
        ):
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=(
                        "implementation revision must project retry worker_commit",
                    ),
                ).to_dict(),
                "record": record,
            }
        try:
            self.store.update(
                contract_execution_id,
                prepared,
                expected_revision=expected_revision,
            )
        except ContractRuntimeError as exc:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=(str(exc),),
                ).to_dict(),
                "record": self.store.get(contract_execution_id),
            }
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "status": "revised",
            "decision": WriteGateDecision(ok=True).to_dict(),
            "record": self.store.get(contract_execution_id),
            "supersedes_implementation_lineage_ref": prior_lineage[
                "implementation_lineage_ref"
            ],
        }

    def revise_failed_qa_observer_dispatch(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
    ) -> dict[str, Any]:
        """Append one source-backed dispatch for a fresh failed-QA rework lane.

        Failed QA may prove that the original RuntimeContext fence is too
        narrow.  Allocating the replacement context must not rewrite the
        historical dispatch, and a timeline dispatch is not ContractRuntime
        authority.  This narrow control-plane revision therefore appends one
        observer-owned dispatch bound to the replacement context.  Exact
        replay is mutation-free; conflicting reuse of the same replacement
        identity is rejected.
        """

        effective_write = dict(write)
        effective_actor_role = _effective_actor_role(
            effective_write,
            actor_role=actor_role,
        )
        if effective_actor_role:
            effective_write["actor_role"] = effective_actor_role
        _enrich_line_instance_fields(effective_write)

        record = self.store.get(contract_execution_id)
        self._raise_if_terminally_retired(record)
        lines = list(record.get("completed_lines") or [])
        failed_qa_index = _active_failed_qa_line_index(
            lines,
            source_record=record,
        )
        payload = (
            dict(effective_write.get("payload"))
            if isinstance(effective_write.get("payload"), Mapping)
            else {}
        )
        authority = (
            dict(payload.get("failed_qa_rework_dispatch_revision_authority"))
            if isinstance(
                payload.get("failed_qa_rework_dispatch_revision_authority"),
                Mapping,
            )
            else {}
        )
        runtime_context_id = _worker_commit_text(
            effective_write,
            "runtime_context_id",
        )
        task_id = _worker_commit_text(effective_write, "task_id")
        parent_task_id = _worker_commit_text(
            effective_write,
            "parent_task_id",
        )
        errors: list[str] = []
        if _record_contract_id(record) not in {"mf_parallel", "mf_parallel.v2"}:
            errors.append("failed-QA dispatch revision requires mf_parallel.v2")
        if effective_actor_role != "observer":
            errors.append("failed-QA dispatch revision requires actor_role=observer")
        if str(effective_write.get("stage_id") or "").strip() != "dispatch":
            errors.append("failed-QA dispatch revision requires stage_id=dispatch")
        if (
            str(effective_write.get("line_id") or "").strip()
            != "observer_dispatch_bounded_workers"
        ):
            errors.append(
                "failed-QA dispatch revision requires observer_dispatch_bounded_workers"
            )
        if (
            str(effective_write.get("evidence_kind") or "").strip()
            != "dispatch_bounded_worker"
        ):
            errors.append(
                "failed-QA dispatch revision requires dispatch_bounded_worker evidence"
            )
        if failed_qa_index < 0:
            errors.append(
                "failed-QA dispatch revision requires active failed independent QA"
            )
        for field, value in (
            ("runtime_context_id", runtime_context_id),
            ("task_id", task_id),
            ("parent_task_id", parent_task_id),
        ):
            if not value:
                errors.append(f"failed-QA dispatch revision requires {field}")
        if authority.get("server_derived") is not True or str(
            authority.get("source") or ""
        ).strip() != "parallel_branch_allocate_failed_qa_rework":
            errors.append(
                "failed-QA dispatch revision requires server-derived allocation authority"
            )
        if str(authority.get("contract_execution_id") or "").strip() != str(
            contract_execution_id
        ).strip():
            errors.append(
                "failed-QA dispatch revision contract_execution_id must match"
            )
        if int(
            authority.get("failed_qa_completed_line_index")
            if authority.get("failed_qa_completed_line_index") is not None
            else -1
        ) != int(failed_qa_index):
            errors.append(
                "failed-QA dispatch revision must bind the active failed QA line"
            )
        if str(authority.get("runtime_context_id") or "").strip() != (
            runtime_context_id
        ):
            errors.append(
                "failed-QA dispatch revision authority runtime_context_id must match"
            )
        if str(authority.get("task_id") or "").strip() != task_id:
            errors.append(
                "failed-QA dispatch revision authority task_id must match"
            )

        matching_dispatches: list[tuple[int, Mapping[str, Any]]] = []
        for index, candidate in enumerate(lines):
            if not isinstance(candidate, Mapping):
                continue
            if (
                str(candidate.get("stage_id") or "").strip() != "dispatch"
                or str(candidate.get("line_id") or "").strip()
                != "observer_dispatch_bounded_workers"
                or str(candidate.get("evidence_kind") or "").strip()
                != "dispatch_bounded_worker"
            ):
                continue
            if (
                _worker_commit_text(candidate, "runtime_context_id")
                == runtime_context_id
                and _worker_commit_text(candidate, "task_id") == task_id
            ):
                matching_dispatches.append((index, candidate))

        current_cycle_matching_dispatches: list[
            tuple[int, Mapping[str, Any]]
        ] = []
        for matching_index, matched in matching_dispatches:
            matched_payload = (
                matched.get("payload")
                if isinstance(matched.get("payload"), Mapping)
                else {}
            )
            matched_revision = (
                matched_payload.get("failed_qa_rework_dispatch_revision")
                if isinstance(
                    matched_payload.get(
                        "failed_qa_rework_dispatch_revision"
                    ),
                    Mapping,
                )
                else {}
            )
            matched_authority = (
                matched_payload.get(
                    "failed_qa_rework_dispatch_revision_authority"
                )
                if isinstance(
                    matched_payload.get(
                        "failed_qa_rework_dispatch_revision_authority"
                    ),
                    Mapping,
                )
                else {}
            )
            if (
                matching_index > failed_qa_index
                and matched_revision.get("append_only_history_preserved") is True
                and matched_revision.get("timeline_projection_authoritative") is False
                and int(
                    matched_revision.get("failed_qa_completed_line_index")
                    if matched_revision.get("failed_qa_completed_line_index")
                    is not None
                    else -1
                )
                == failed_qa_index
                and str(matched_revision.get("runtime_context_id") or "").strip()
                == runtime_context_id
                and str(matched_revision.get("task_id") or "").strip() == task_id
                and matched_authority.get("server_derived") is True
                and str(matched_authority.get("source") or "").strip()
                == "parallel_branch_allocate_failed_qa_rework"
                and str(
                    matched_authority.get("contract_execution_id") or ""
                ).strip()
                == str(contract_execution_id).strip()
                and int(
                    matched_authority.get("failed_qa_completed_line_index")
                    if matched_authority.get("failed_qa_completed_line_index")
                    is not None
                    else -1
                )
                == failed_qa_index
                and str(
                    matched_authority.get("runtime_context_id") or ""
                ).strip()
                == runtime_context_id
                and str(matched_authority.get("task_id") or "").strip()
                == task_id
            ):
                current_cycle_matching_dispatches.append(
                    (matching_index, matched)
                )

        exact_replay = False
        if len(current_cycle_matching_dispatches) > 1:
            errors.append(
                "failed-QA dispatch revision has multiple current-cycle "
                "replacement RuntimeContext authorities"
            )
        elif current_cycle_matching_dispatches:
            _matching_index, matched = current_cycle_matching_dispatches[0]
            exact_replay = (
                _worker_commit_text(matched, "parent_task_id")
                == parent_task_id
                and _worker_commit_text(matched, "worker_id")
                == _worker_commit_text(effective_write, "worker_id")
                and _worker_commit_text(matched, "worker_slot_id")
                == _worker_commit_text(effective_write, "worker_slot_id")
                and sorted(
                    set(_worker_commit_strings(matched, "owned_files"))
                )
                == sorted(
                    set(
                        _worker_commit_strings(
                            effective_write,
                            "owned_files",
                        )
                    )
                )
                and str(
                    _first_deep_contract_value(
                        matched,
                        "route_token_ref",
                    )
                    or ""
                ).strip()
                == str(
                    _first_deep_contract_value(
                        effective_write,
                        "route_token_ref",
                    )
                    or ""
                ).strip()
            )
            if not exact_replay:
                errors.append(
                    "failed-QA dispatch revision conflicts with the current-cycle "
                    "replacement RuntimeContext dispatch"
                )

        if errors:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=tuple(dict.fromkeys(errors)),
                ).to_dict(),
                "record": record,
            }
        if exact_replay:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": True,
                "status": "already_completed",
                "decision": WriteGateDecision(ok=True).to_dict(),
                "record": record,
                "contract_runtime_line_mutated": False,
                "completed_line_already_recorded": True,
            }

        payload["failed_qa_rework_dispatch_revision"] = {
            "schema_version": (
                "contract_runtime.failed_qa_rework_dispatch_revision.v1"
            ),
            "source": "parallel_branch_allocate",
            "failed_qa_completed_line_index": failed_qa_index,
            "runtime_context_id": runtime_context_id,
            "task_id": task_id,
            "append_only_history_preserved": True,
            "timeline_projection_authoritative": False,
        }
        effective_write["payload"] = payload
        written_line = _line_evidence_from_write(
            effective_write,
            effective_actor_role,
        )
        completed_lines = [*lines, written_line]
        expected_revision = int(record.get("execution_state_revision") or 1)
        candidate = dict(record)
        candidate["completed_lines"] = completed_lines
        candidate["execution_state_revision"] = expected_revision + 1
        prepared = self._record_view(
            candidate,
            actor_role=effective_actor_role,
            completed_lines=completed_lines,
        )
        try:
            self.store.update(
                contract_execution_id,
                prepared,
                expected_revision=expected_revision,
            )
        except ContractRuntimeError as exc:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=(str(exc),),
                ).to_dict(),
                "record": self.store.get(contract_execution_id),
            }
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "status": "revised",
            "decision": WriteGateDecision(ok=True).to_dict(),
            "record": self.store.get(contract_execution_id),
            "contract_runtime_line_mutated": True,
            "append_only_history_preserved": True,
        }

    def revise_precommit_worker_implementation(
        self,
        contract_execution_id: str,
        write: Mapping[str, Any],
        *,
        actor_role: str | None = None,
    ) -> dict[str, Any]:
        """Append one canonical implementation correction before worker commit.

        This is not a duplicate-line or rewind facility.  It is a bounded
        append-only correction for the narrow case where the authenticated
        worker has already recorded ``worker_implementation``, the live
        ContractRuntime line is ``worker_commit``, and no worker commit exists.
        Server-derived immutable git and graph authority must accompany the
        write.  Once a correction is recorded, only an exact idempotent replay
        is accepted; a different HEAD must not recursively revise the lineage.
        """

        effective_write = dict(write)
        effective_actor_role = _effective_actor_role(
            effective_write,
            actor_role=actor_role,
        )
        if effective_actor_role:
            effective_write["actor_role"] = effective_actor_role
        _enrich_line_instance_fields(effective_write)

        record = self.store.get(contract_execution_id)
        self._raise_if_terminally_retired(record)
        lines = list(record.get("completed_lines") or [])
        payload = (
            dict(effective_write.get("payload"))
            if isinstance(effective_write.get("payload"), Mapping)
            else {}
        )
        authority = (
            dict(payload.get("canonical_precommit_lineage_revision_authority"))
            if isinstance(
                payload.get("canonical_precommit_lineage_revision_authority"),
                Mapping,
            )
            else {}
        )
        graph_evidence = (
            dict(payload.get("graph_trace_db_evidence"))
            if isinstance(payload.get("graph_trace_db_evidence"), Mapping)
            else {}
        )
        rejoin_marker = (
            dict(payload.get("precommit_correction_rejoin_marker"))
            if isinstance(
                payload.get("precommit_correction_rejoin_marker"),
                Mapping,
            )
            else {}
        )
        guide = self._record_view(
            record,
            actor_role=effective_actor_role,
            completed_lines=lines,
        )["runtime_guide"]
        next_action = (
            guide.get("next_legal_action")
            if isinstance(guide.get("next_legal_action"), Mapping)
            else {}
        )

        runtime_context_id = _worker_commit_text(
            effective_write,
            "runtime_context_id",
        )
        task_id = _worker_commit_text(effective_write, "task_id")
        prior_implementation: Mapping[str, Any] | None = None
        prior_index = -1
        for index in range(len(lines) - 1, -1, -1):
            candidate = lines[index]
            if not isinstance(candidate, Mapping):
                continue
            if str(candidate.get("line_id") or "").strip() != "worker_implementation":
                continue
            if runtime_context_id and _worker_commit_text(
                candidate,
                "runtime_context_id",
            ) != runtime_context_id:
                continue
            if task_id and _worker_commit_text(candidate, "task_id") != task_id:
                continue
            prior_implementation = candidate
            prior_index = index
            break

        matching_worker_commit_exists = any(
            isinstance(candidate, Mapping)
            and str(candidate.get("line_id") or "").strip() == "worker_commit"
            and (
                not runtime_context_id
                or _worker_commit_text(candidate, "runtime_context_id")
                == runtime_context_id
            )
            and (
                not task_id
                or _worker_commit_text(candidate, "task_id") == task_id
            )
            for candidate in lines
        )
        errors: list[str] = []
        if _record_contract_id(record) not in {"mf_parallel", "mf_parallel.v2"}:
            errors.append("precommit implementation correction requires mf_parallel.v2")
        if effective_actor_role != "mf_sub":
            errors.append(
                "precommit implementation correction requires actor_role=mf_sub"
            )
        if str(effective_write.get("line_id") or "").strip() != (
            "worker_implementation"
        ):
            errors.append(
                "precommit implementation correction requires worker_implementation"
            )
        if str(effective_write.get("evidence_kind") or "").strip() != (
            "implementation"
        ):
            errors.append(
                "precommit implementation correction requires implementation evidence"
            )
        if _active_failed_qa_line_index(lines, source_record=record) >= 0:
            errors.append(
                "precommit implementation correction cannot replace failed-QA rework"
            )
        if str(next_action.get("line_id") or "").strip() != "worker_commit":
            errors.append(
                "precommit implementation correction is only legal before worker_commit"
            )
        if prior_implementation is None:
            errors.append(
                "precommit implementation correction requires a prior worker_implementation"
            )
        if matching_worker_commit_exists:
            errors.append(
                "precommit implementation correction is closed after worker_commit"
            )

        identity_fields = (
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "worker_id",
            "worker_slot_id",
            "target_project_root",
            "fence_token_hash",
        )
        if prior_implementation is not None:
            for field in identity_fields:
                prior_value = _worker_commit_text(prior_implementation, field)
                revised_value = _worker_commit_text(effective_write, field)
                if prior_value and revised_value != prior_value:
                    errors.append(
                        "precommit implementation correction "
                        f"{field} must match prior implementation"
                    )

        prior_session_token_ref = (
            _worker_commit_text(prior_implementation, "session_token_ref")
            if prior_implementation is not None
            else ""
        )
        revised_session_token_ref = _worker_commit_text(
            effective_write,
            "session_token_ref",
        )
        session_token_ref_rotation = (
            dict(rejoin_marker.get("session_token_ref_rotation"))
            if isinstance(
                rejoin_marker.get("session_token_ref_rotation"),
                Mapping,
            )
            else {}
        )
        session_token_ref_rotated = bool(
            prior_session_token_ref
            and revised_session_token_ref != prior_session_token_ref
        )
        if session_token_ref_rotated:
            rotation_errors: list[str] = []
            if not revised_session_token_ref:
                rotation_errors.append("active session_token_ref is required")
            if (
                rejoin_marker.get("server_derived") is not True
                or str(rejoin_marker.get("source") or "").strip()
                != "accepted_runtime_context_rejoin_event"
            ):
                rotation_errors.append(
                    "server-derived same-worker rejoin authority is required"
                )
            rejoin_event_ref = str(
                rejoin_marker.get("rejoin_event_ref") or ""
            ).strip()
            if not rejoin_event_ref.startswith("timeline:"):
                rotation_errors.append("audited rejoin event ref is required")
            if (
                session_token_ref_rotation.get("server_derived") is not True
                or str(
                    session_token_ref_rotation.get("source") or ""
                ).strip()
                != "accepted_runtime_context_rejoin_event"
            ):
                rotation_errors.append(
                    "server-derived session rotation is required"
                )
            if str(
                session_token_ref_rotation.get("rejoin_event_ref") or ""
            ).strip() != rejoin_event_ref:
                rotation_errors.append("session rotation rejoin event must match")
            if str(
                session_token_ref_rotation.get("prior_session_token_ref") or ""
            ).strip() != prior_session_token_ref:
                rotation_errors.append("prior session_token_ref must match")
            if str(
                session_token_ref_rotation.get("active_session_token_ref") or ""
            ).strip() != revised_session_token_ref:
                rotation_errors.append("active session_token_ref must match")
            for field in (
                "runtime_context_id",
                "task_id",
                "parent_task_id",
                "worker_id",
                "worker_slot_id",
                "target_project_root",
                "fence_token_hash",
            ):
                expected_value = _worker_commit_text(effective_write, field)
                marker_value = str(
                    session_token_ref_rotation.get(field) or ""
                ).strip()
                if expected_value and marker_value != expected_value:
                    rotation_errors.append(f"rejoin {field} must match")
            if str(
                session_token_ref_rotation.get("contract_execution_id") or ""
            ).strip() != contract_execution_id:
                rotation_errors.append("rejoin contract_execution_id must match")
            if rotation_errors:
                errors.append(
                    "precommit implementation correction session_token_ref "
                    "must match prior implementation or an audited same-worker "
                    "rejoin: "
                    + "; ".join(rotation_errors)
                )

        commit_sha = str(effective_write.get("commit_sha") or "").strip()
        changed_files = sorted(
            set(_worker_commit_strings(effective_write, "changed_files"))
        )
        graph_trace_ids = sorted(
            set(
                _worker_commit_strings(
                    effective_write,
                    "graph_trace_ids",
                    "graph_query_trace_ids",
                    "verified_trace_ids",
                )
            )
        )
        if not _WORKER_COMMIT_SHA_RE.fullmatch(commit_sha):
            errors.append(
                "precommit implementation correction requires a full immutable commit_sha"
            )
        if not changed_files:
            errors.append(
                "precommit implementation correction requires cumulative changed_files"
            )
        if not graph_trace_ids:
            errors.append(
                "precommit implementation correction requires graph_trace_ids"
            )
        if authority.get("server_derived") is not True or str(
            authority.get("source") or ""
        ).strip() != "runtime_context_clean_cumulative_git_precommit_correction":
            errors.append(
                "precommit implementation correction requires server-derived git authority"
            )
        if (
            authority.get("correction_intent_verified") is not True
            or str(
                authority.get("correction_intent_schema_version") or ""
            ).strip()
            != "runtime_context.precommit_implementation_correction_intent.v1"
            or str(authority.get("correction_intent_action") or "").strip()
            != "revise_precommit_worker_implementation"
        ):
            errors.append(
                "precommit implementation correction requires server-verified correction intent"
            )
        if authority.get("clean_worktree") is not True:
            errors.append(
                "precommit implementation correction requires a clean worktree"
            )
        if str(authority.get("actual_head_commit") or "").strip() != commit_sha:
            errors.append(
                "precommit implementation correction commit must match actual worker HEAD"
            )
        if sorted(set(authority.get("cumulative_changed_files") or [])) != (
            changed_files
        ):
            errors.append(
                "precommit implementation correction files must match cumulative runtime diff"
            )
        owned_files = set(authority.get("owned_files") or [])
        fence_containment = _worker_fence_containment(
            changed_files,
            sorted(owned_files),
            repository_root=str(
                authority.get("fence_repository_root")
                or effective_write.get("target_project_root")
                or payload.get("target_project_root")
                or ""
            ).strip(),
        )
        if not fence_containment["ok"]:
            errors.append(
                "precommit implementation correction files must remain inside the worker fence"
            )
        if graph_evidence.get("db_verified") is not True or sorted(
            set(graph_evidence.get("verified_trace_ids") or [])
        ) != graph_trace_ids:
            errors.append(
                "precommit implementation correction requires exact DB-verified graph traces"
            )

        prior_payload = (
            prior_implementation.get("payload")
            if prior_implementation is not None
            and isinstance(prior_implementation.get("payload"), Mapping)
            else {}
        )
        prior_correction = (
            prior_payload.get("canonical_precommit_lineage_revision")
            if isinstance(
                prior_payload.get("canonical_precommit_lineage_revision"),
                Mapping,
            )
            else {}
        )
        prior_lineage = _worker_implementation_lineage(
            record,
            prior_implementation or {},
        )
        expected_intent_prior_lineage_ref = str(
            prior_correction.get(
                "supersedes_implementation_lineage_ref"
            )
            or prior_lineage.get("implementation_lineage_ref")
            or ""
        ).strip()
        if str(
            authority.get("prior_implementation_lineage_ref") or ""
        ).strip() != expected_intent_prior_lineage_ref:
            errors.append(
                "precommit implementation correction intent must bind the prior implementation lineage"
            )
        if prior_correction and not matching_worker_commit_exists:
            prior_authority = (
                prior_payload.get(
                    "canonical_precommit_lineage_revision_authority"
                )
                if isinstance(
                    prior_payload.get(
                        "canonical_precommit_lineage_revision_authority"
                    ),
                    Mapping,
                )
                else {}
            )
            prior_files = sorted(
                set(
                    _worker_commit_strings(
                        prior_implementation,
                        "changed_files",
                    )
                )
            )
            prior_trace_ids = sorted(
                set(
                    _worker_commit_strings(
                        prior_implementation,
                        "graph_trace_ids",
                        "graph_query_trace_ids",
                        "verified_trace_ids",
                    )
                )
            )
            if (
                not errors
                and str(prior_authority.get("actual_head_commit") or "").strip()
                == commit_sha
                and prior_files == changed_files
                and prior_trace_ids == graph_trace_ids
            ):
                return {
                    "schema_version": "contract_runtime_write_result.v1",
                    "ok": True,
                    "status": "already_completed",
                    "decision": WriteGateDecision(ok=True).to_dict(),
                    "record": record,
                    "supersedes_implementation_lineage_ref": str(
                        prior_correction.get(
                            "supersedes_implementation_lineage_ref"
                        )
                        or ""
                    ),
                }
            errors.append(
                "precommit implementation correction already recorded; "
                "different-HEAD replay is forbidden"
            )

        if errors:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=tuple(dict.fromkeys(errors)),
                ).to_dict(),
                "record": record,
            }

        payload["canonical_precommit_lineage_revision"] = {
            "schema_version": (
                "contract_runtime.worker_implementation_precommit_revision.v1"
            ),
            "source": "server_verified_precommit_correction",
            "superseded_completed_line_index": prior_index,
            "supersedes_implementation_lineage_ref": prior_lineage[
                "implementation_lineage_ref"
            ],
            "commit_sha": commit_sha,
            "append_only_history_preserved": True,
            "single_correction_boundary": True,
            "raw_session_tokens_persisted": False,
            "session_token_ref_rotation": {
                "applied": session_token_ref_rotated,
                "source": (
                    "accepted_runtime_context_rejoin_event"
                    if session_token_ref_rotated
                    else "same_session_token_ref"
                ),
                "rejoin_event_ref": (
                    str(rejoin_marker.get("rejoin_event_ref") or "").strip()
                    if session_token_ref_rotated
                    else ""
                ),
                "raw_session_tokens_persisted": False,
            },
        }
        effective_write["payload"] = payload
        written_line = _line_evidence_from_write(
            effective_write,
            effective_actor_role,
        )
        expected_revision = int(record.get("execution_state_revision") or 1)
        updated_record = dict(record)
        updated_record["completed_lines"] = [*lines, written_line]
        updated_record["execution_state_revision"] = expected_revision + 1
        updated_record = self._record_view(
            updated_record,
            actor_role=effective_actor_role,
            completed_lines=updated_record["completed_lines"],
        )
        try:
            self.store.update(
                contract_execution_id,
                updated_record,
                expected_revision=expected_revision,
            )
        except ContractRuntimeError as exc:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": WriteGateDecision(
                    ok=False,
                    errors=(str(exc),),
                ).to_dict(),
                "record": self.store.get(contract_execution_id),
            }
        return {
            "schema_version": "contract_runtime_write_result.v1",
            "ok": True,
            "status": "revised",
            "decision": WriteGateDecision(ok=True).to_dict(),
            "record": deepcopy(updated_record),
            "supersedes_implementation_lineage_ref": prior_lineage[
                "implementation_lineage_ref"
            ],
        }

    def bypass_current_line(
        self,
        contract_execution_id: str,
        bypass: Mapping[str, Any],
        *,
        actor_role: str | None = None,
        projected_completed_lines: Sequence[Mapping[str, Any]] | None = None,
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Waive the current line without PASS or downstream diagnostic fanout."""

        record = self.store.get(contract_execution_id)
        self._raise_if_terminally_retired(record)
        active_no_pass_generation = _contract_runtime_no_pass_generation(record)
        request = dict(bypass)
        effective_actor_role = _effective_actor_role(request, actor_role=actor_role)
        evidence_refs = _sanitize_line_evidence_value(request.get("evidence_refs") or [])
        continuation_authority = _sanitize_line_evidence_value(
            request.get("continuation_authority") or {}
        )
        request_fields = {
            "bypass_identity": str(request.get("bypass_identity") or "").strip(),
            "line_id": str(request.get("line_id") or "").strip(),
            "stage_id": str(request.get("stage_id") or "").strip(),
            "execution_state_revision": request.get("execution_state_revision"),
            "diagnostic_backlog_id": str(
                request.get("diagnostic_backlog_id") or ""
            ).strip(),
            "classification": str(request.get("classification") or "").strip(),
            "reason": str(request.get("reason") or "").strip(),
            "decision": str(request.get("decision") or "").strip(),
            "actor_role": effective_actor_role,
            "evidence_refs": evidence_refs,
            "continuation_authority": continuation_authority,
        }
        legacy_worker_commit_compatibility = bool(
            active_no_pass_generation
            and active_no_pass_generation.get("root_generation_persisted")
            is not True
            and request_fields["line_id"] == "worker_commit"
        )
        if legacy_worker_commit_compatibility:
            # Historical v1 executions already minted a distinct, strictly
            # validated worker-commit diagnostic.  Keep that immutable v2/v3
            # continuation path readable; later gates enter single-root mode.
            active_no_pass_generation = {}
        request_hash = stable_sha256(request_fields)

        def rejected(*errors: str, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
            return {
                "schema_version": "contract_runtime_bypass_result.v1",
                "ok": False,
                "idempotent": False,
                "decision": {
                    "ok": False,
                    "errors": list(errors),
                    "no_pass_claim": True,
                },
                "record": dict(current or record),
            }

        visible_lines = list(record.get("completed_lines") or [])
        visible_lines.extend(list(projected_completed_lines or []))
        for line in reversed(visible_lines):
            payload = line.get("payload") if isinstance(line.get("payload"), Mapping) else {}
            if (
                str(line.get("evidence_kind") or "") == "contract_line_bypass"
                and str(payload.get("bypass_identity") or "")
                == request_fields["bypass_identity"]
            ):
                if str(payload.get("request_hash") or "") != request_hash:
                    return rejected("bypass_identity_conflict")
                return {
                    "schema_version": "contract_runtime_bypass_result.v1",
                    "ok": True,
                    "idempotent": True,
                    "decision": {
                        "ok": True,
                        "errors": [],
                        "disposition": "proceeded_with_exception",
                        "no_pass_claim": True,
                    },
                    "written_line": deepcopy(dict(line)),
                    "record": self.store.get(contract_execution_id),
                }

        missing = [
            key
            for key in (
                "bypass_identity",
                "line_id",
                "diagnostic_backlog_id",
                "classification",
                "reason",
                "decision",
            )
            if not request_fields[key]
        ]
        if missing:
            return rejected(*(f"missing {key}" for key in missing))
        if active_no_pass_generation and request_fields[
            "diagnostic_backlog_id"
        ] != str(
            active_no_pass_generation.get("root_diagnostic_backlog_id") or ""
        ):
            return rejected(
                "active no-PASS generation requires its root diagnostic"
            )
        if effective_actor_role not in {"observer", "qa"}:
            return rejected("bypass actor_role must be observer or qa")
        try:
            expected_revision = int(request_fields["execution_state_revision"])
        except (TypeError, ValueError):
            return rejected("invalid execution_state_revision")
        actual_revision = int(record.get("execution_state_revision") or 1)
        if expected_revision != actual_revision:
            return rejected("execution_state_revision mismatch")

        refreshed = record
        gate_record: Mapping[str, Any] = self._record_view(
            refreshed,
            actor_role=effective_actor_role,
            completed_lines=refreshed.get("completed_lines") or [],
        )
        guide = gate_record["runtime_guide"]
        if projected_completed_lines is not None:
            gate_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_completed_lines,
                projection=projection,
            )
            guide = gate_record["runtime_guide"]
        next_action = guide.get("next_legal_action")
        if not isinstance(next_action, Mapping):
            return rejected("contract has no current line to bypass", current=gate_record)
        if request_fields["line_id"] != str(next_action.get("line_id") or ""):
            return rejected("bypass does not match current line", current=gate_record)
        if request_fields["stage_id"] and request_fields["stage_id"] != str(
            next_action.get("stage_id") or ""
        ):
            return rejected("bypass does not match current stage", current=gate_record)
        requested_guide_hash = str(request.get("runtime_guide_hash") or "").strip()
        if requested_guide_hash and requested_guide_hash != str(
            guide.get("runtime_guide_hash") or ""
        ):
            return rejected("runtime_guide_hash mismatch", current=gate_record)

        payload = {
            "schema_version": "contract_line_bypass.v1",
            "bypass_identity": request_fields["bypass_identity"],
            "request_hash": request_hash,
            "source_backlog_id": str(record.get("backlog_id") or ""),
            "diagnostic_backlog_id": request_fields["diagnostic_backlog_id"],
            "classification": request_fields["classification"],
            "reason": request_fields["reason"],
            "decision": request_fields["decision"],
            "blocked_owner_role": str(next_action.get("owner_role") or ""),
            "blocked_evidence_kind": str(next_action.get("evidence_kind") or ""),
            "execution_state_revision": expected_revision,
            "disposition": "proceeded_with_exception",
            "no_pass_claim": True,
            "evidence_refs": evidence_refs,
        }
        if active_no_pass_generation:
            payload["no_pass_generation"] = {
                "schema_version": "contract_line_bypass_generation_link.v1",
                "generation_id": str(
                    active_no_pass_generation.get("generation_id") or ""
                ),
                "role": "inherited_gate",
                "root_bypass_identity": str(
                    active_no_pass_generation.get("root_bypass_identity") or ""
                ),
                "root_diagnostic_backlog_id": str(
                    active_no_pass_generation.get(
                        "root_diagnostic_backlog_id"
                    )
                    or ""
                ),
                "root_stage_id": str(
                    active_no_pass_generation.get("root_stage_id") or ""
                ),
                "root_line_id": str(
                    active_no_pass_generation.get("root_line_id") or ""
                ),
                "root_line_instance_id": str(
                    active_no_pass_generation.get("root_line_instance_id")
                    or ""
                ),
                "root_classification": str(
                    active_no_pass_generation.get("root_classification") or ""
                ),
                "root_execution_state_revision": int(
                    active_no_pass_generation.get(
                        "root_execution_state_revision"
                    )
                    or 0
                ),
                "gate_reason_code": request_fields["classification"],
                "gate_reason": request_fields["reason"],
                "original_gate_evidence_status": (
                    "not_authoritatively_satisfied_due_to_active_no_pass_generation"
                ),
                "diagnostic_created": False,
                "no_pass_claim": True,
                "authoritative_pass_synthesized": False,
            }
        else:
            digest = hashlib.sha256(
                canonical_json(
                    {
                        "project_id": str(record.get("project_id") or ""),
                        "source_backlog_id": str(record.get("backlog_id") or ""),
                        "contract_execution_id": contract_execution_id,
                        "root_bypass_identity": request_fields[
                            "bypass_identity"
                        ],
                        "root_line_instance_id": str(
                            next_action.get("line_instance_id") or ""
                        ),
                    }
                ).encode("utf-8")
            ).hexdigest()[:20]
            payload["no_pass_generation"] = {
                "schema_version": "contract_line_bypass_generation_link.v1",
                "generation_id": f"bypassgen-{digest}",
                "role": "root",
                "root_bypass_identity": request_fields["bypass_identity"],
                "root_diagnostic_backlog_id": request_fields[
                    "diagnostic_backlog_id"
                ],
                "root_stage_id": str(next_action.get("stage_id") or ""),
                "root_line_id": request_fields["line_id"],
                "root_line_instance_id": str(
                    next_action.get("line_instance_id") or ""
                ),
                "root_classification": request_fields["classification"],
                "root_execution_state_revision": expected_revision,
                "gate_reason_code": request_fields["classification"],
                "gate_reason": request_fields["reason"],
                "original_gate_evidence_status": "blocked_at_root",
                "diagnostic_created": True,
                "no_pass_claim": True,
                "authoritative_pass_synthesized": False,
            }
        if continuation_authority:
            payload["continuation_authority"] = continuation_authority
        written_line = {
            "stage_id": str(next_action.get("stage_id") or ""),
            "line_id": request_fields["line_id"],
            "actor_role": effective_actor_role,
            "evidence_kind": "contract_line_bypass",
            "status": "waived",
            "disposition": "proceeded_with_exception",
            "no_pass_claim": True,
            "payload": payload,
        }
        if isinstance(continuation_authority, Mapping):
            for key in (
                "commit_sha",
                "runtime_context_id",
                "task_id",
                "parent_task_id",
            ):
                value = str(continuation_authority.get(key) or "").strip()
                if value:
                    written_line[key] = value
        line_instance_id = str(next_action.get("line_instance_id") or "").strip()
        if line_instance_id:
            written_line["line_instance_id"] = line_instance_id

        completed_lines = list(refreshed.get("completed_lines") or [])
        completed_lines.append(written_line)
        refreshed["completed_lines"] = completed_lines
        refreshed["execution_state_revision"] = actual_revision + 1
        refreshed = self._record_view(
            refreshed,
            actor_role=effective_actor_role,
            completed_lines=completed_lines,
        )
        try:
            self.store.update(
                contract_execution_id,
                refreshed,
                expected_revision=actual_revision,
            )
        except ContractRuntimeError as exc:
            return rejected(str(exc), current=self.store.get(contract_execution_id))

        result_record = deepcopy(refreshed)
        if projected_completed_lines is not None:
            projected_after = list(projected_completed_lines)
            projected_after.append(written_line)
            result_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_after,
                projection=projection,
            )
        return {
            "schema_version": "contract_runtime_bypass_result.v1",
            "ok": True,
            "idempotent": False,
            "decision": {
                "ok": True,
                "errors": [],
                "disposition": "proceeded_with_exception",
                "no_pass_claim": True,
            },
            "written_line": deepcopy(written_line),
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
        self._raise_if_terminally_retired(record)
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
        refreshed = record
        gate_record = self._record_view(
            refreshed,
            actor_role=effective_actor_role,
            completed_lines=refreshed.get("completed_lines") or [],
        )
        guide = gate_record["runtime_guide"]
        use_completed_line_projection = (
            projected_completed_lines is not None
            and str(effective_write.get("line_id") or "").strip()
            != "worker_commit"
        )
        if use_completed_line_projection:
            gate_record = self.projected_record(
                contract_execution_id,
                actor_role=effective_actor_role,
                completed_lines=projected_completed_lines,
                projection=projection,
            )
            guide = gate_record["runtime_guide"]
        gate_state, gate_guide = self.mf_parallel_atomic_lane_gate_view(
            gate_record,
            guide,
            effective_write,
            source_record=refreshed,
            projection=projection,
        )
        gate_state = {
            **dict(gate_state),
            "metadata": deepcopy(dict(refreshed.get("metadata") or {})),
            "completed_lines": deepcopy(
                list(refreshed.get("completed_lines") or [])
            ),
        }
        _bind_mf_parallel_atomic_lane_runtime_guide_hash(
            effective_write,
            gate_guide,
        )
        gate_decision = self.gate_kernel.precheck(
            definition,
            action="submit_line",
            actor_role=effective_actor_role,
            execution_state=gate_state,
            runtime_guide=gate_guide,
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
                refreshed,
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
        self._authoritative_common_rule_join(record, definition)
        return definition

    def _authoritative_common_rule_join(
        self,
        record: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> dict[str, Any]:
        resolved = self.registry.resolve_common_rule_applicability(definition)
        pinned = record.get("authoritative_common_rule_join")
        if isinstance(pinned, Mapping):
            _assert_hash(
                "common_rule_authority_hash",
                pinned.get("authority_hash"),
                resolved.get("authority_hash"),
                record=record,
                definition=definition,
            )
        elif resolved.get("authoritative") is True:
            raise ContractRuntimeError(
                "common_rule_authority_join_missing_from_pinned_execution: "
                "a resolved common Rule join must be pinned when the execution "
                "is created; historical evidence is not backfilled"
            )
        return resolved

    def _terminal_retirement_for_record(
        self,
        record: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        registry = getattr(self, "registry", None)
        if registry is None:
            return None
        return registry.terminal_retirement_for(
            str(record.get("contract_id") or ""),
            version=str(record.get("version") or ""),
        )

    def _raise_if_terminally_retired(self, record: Mapping[str, Any]) -> None:
        retirement = self._terminal_retirement_for_record(record)
        if retirement is not None:
            raise ContractRetirementError(retirement)


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
    """Hard-block the finitely migrated source-backed root policies.

    The broader Gate Kernel rollout runs in shadow mode for existing facades so
    contract_add/update/hotfix can be migrated without breaking legacy tests in
    one step. Parallel worker and Batch parent orchestration are guide-bound
    successors, so their declared root denials are authoritative now.
    """

    contract_id = str(definition.get("contract_id") or "")
    aliases = {str(item) for item in definition.get("compat_aliases") or []}
    return (
        contract_id == "mf_parallel"
        or contract_id == "mf_batch_parallel.v1"
        or "mf_parallel.v2" in aliases
        or "mf_parallel.v1" in aliases
        or "mf_batch_parallel" in aliases
    )


def _guide_bound_server_projection_only(definition: Mapping[str, Any]) -> bool:
    """Keep the finite Batch definition on its declared existing server facade."""

    contract_id = str(definition.get("contract_id") or "").strip()
    aliases = {str(item).strip() for item in definition.get("compat_aliases") or []}
    system_layer = (
        definition.get("system_layer")
        if isinstance(definition.get("system_layer"), Mapping)
        else {}
    )
    entrypoint = (
        system_layer.get("entrypoint_policy")
        if isinstance(system_layer.get("entrypoint_policy"), Mapping)
        else {}
    )
    metadata = definition.get("metadata")
    gate_bindings = (
        metadata.get("gate_bindings")
        if isinstance(metadata, Mapping)
        and isinstance(metadata.get("gate_bindings"), Mapping)
        else {}
    )
    return (
        (contract_id == "mf_batch_parallel.v1" or "mf_batch_parallel" in aliases)
        and entrypoint.get("allow_root_start") is False
        and entrypoint.get("requires_parent_execution") is True
        and gate_bindings.get("new_predicates_added") is False
        and gate_bindings.get("hidden_server_inference_allowed") is False
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


# Public, explicit safe vocabulary for ContractRuntime completed-line evidence.
# HTTP/MCP facades may use this allowlist when rebuilding a line write, but raw
# authentication tokens remain transport-only and are intentionally absent.
LINE_EVIDENCE_OPTIONAL_FIELDS = (
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
    "tests",
    "test_results",
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
    "implementation_lineage_ref",
    "worker_implementation_lineage",
    "implementation_event_ref",
    "worker_commit_sha",
    "worker_session_id",
    "filer_principal",
    "session_token_ref",
    "fence_token_hash",
    "owned_files",
    "changed_files",
    "owned_changed_files",
    "worker_changed_files",
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
# Backward-compatible private alias for internal consumers that predate the
# shared facade/runtime contract name.
_LINE_EVIDENCE_OPTIONAL_FIELDS = LINE_EVIDENCE_OPTIONAL_FIELDS
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
    qa_status = str(write.get("status") or "").strip().lower()
    provenance["completion_status_gate"] = {
        "schema_version": _QA_COMPLETION_STATUS_GATE_SCHEMA_VERSION,
        "source": "contract_runtime_line_write_normalization",
        "top_level_status_present": bool(qa_status),
        "top_level_status_passing": qa_status in _QA_COMPLETION_PASSING_STATUSES,
        "normalized_status": qa_status,
        "nested_payload_decision_satisfies": False,
        "server_derived": True,
    }
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


def _attach_line_bypass_guidance(
    guide: dict[str, Any],
    *,
    record: Mapping[str, Any],
    reader_role: str,
) -> None:
    """Expose the existing strict line-bypass row lifecycle in copy-safe form."""

    next_action = guide.get("next_legal_action")
    if not isinstance(next_action, Mapping):
        return
    contract_execution_id = str(
        record.get("contract_execution_id") or ""
    ).strip()
    source_backlog_id = str(record.get("backlog_id") or "").strip()
    stage_id = str(next_action.get("stage_id") or "").strip()
    line_id = str(next_action.get("line_id") or "").strip()
    if not contract_execution_id or not source_backlog_id or not line_id:
        return
    execution_state_revision = int(
        next_action.get("execution_state_revision")
        or record.get("execution_state_revision")
        or 1
    )
    line_instance_id = str(
        next_action.get("line_instance_id") or ""
    ).strip()
    identity_parts = [
        "bypass",
        contract_execution_id,
        f"revision-{execution_state_revision}",
        stage_id or "stage",
        line_id,
    ]
    if line_instance_id:
        identity_parts.append(line_instance_id)
    bypass_identity = ":".join(identity_parts)
    active_no_pass_generation = _contract_runtime_no_pass_generation(record)
    if (
        active_no_pass_generation
        and active_no_pass_generation.get("root_generation_persisted") is not True
        and line_id == "worker_commit"
    ):
        active_no_pass_generation = {}
    diagnostic_suffix = hashlib.sha256(
        bypass_identity.encode("utf-8")
    ).hexdigest()[:16].upper()
    deterministic_diagnostic_id = (
        f"AC-CONTRACT-LINE-BYPASS-{diagnostic_suffix}"
    )
    runtime_guide_hash = str(guide.get("runtime_guide_hash") or "")
    evidence_refs = [
        f"backlog:{source_backlog_id}",
        (
            f"contract-runtime:{contract_execution_id}:"
            f"revision:{execution_state_revision}"
        ),
        "<blocked-line-evidence-ref>",
    ]
    common_body = {
        "bypass_identity": bypass_identity,
        "stage_id": stage_id,
        "line_id": line_id,
        "execution_state_revision": execution_state_revision,
        "runtime_guide_hash": runtime_guide_hash,
        "classification": "<process_guide|system_logic|environment>",
        "reason": "<diagnosis of this exact blocked line>",
        "decision": (
            "<audited continuation decision; do not claim the blocked line passed>"
        ),
        "evidence_refs": evidence_refs,
    }
    if active_no_pass_generation:
        common_body.update(
            {
                "diagnostic_backlog_id": str(
                    active_no_pass_generation.get(
                        "root_diagnostic_backlog_id"
                    )
                    or ""
                ),
                "classification": "<gate-specific inherited reason code>",
                "reason": (
                    "<why this exact gate cannot produce authoritative evidence "
                    "under the active no-PASS generation>"
                ),
                "decision": (
                    "inherit the root no-PASS disposition; do not claim this gate passed"
                ),
            }
        )
    exact_binding = {
        "source_backlog_id": source_backlog_id,
        "contract_execution_id": contract_execution_id,
        "line_id": line_id,
    }
    guide["line_bypass_guidance"] = {
        "schema_version": "contract_runtime.line_bypass_guidance.v1",
        "status": (
            "active_generation_inherited_bypass_required"
            if active_no_pass_generation
            else "available_for_current_line"
        ),
        "copy_safe": True,
        "endpoint": (
            f"/api/projects/{record.get('project_id')}/contract-runtime/"
            f"{contract_execution_id}/line-bypasses"
        ),
        "method": "POST",
        "current_line_binding": {
            **exact_binding,
            "stage_id": stage_id,
            "line_instance_id": line_instance_id,
            "execution_state_revision": execution_state_revision,
            "runtime_guide_hash": runtime_guide_hash,
        },
        "authorization": {
            "allowed_actor_roles": ["observer", "qa"],
            "reader_role": str(reader_role or ""),
            "reader_role_authorized": str(reader_role or "")
            in {"observer", "qa"},
            "body_actor_role_is_not_authorization": True,
        },
        "bypass_identity": {
            "recommended_value": bypass_identity,
            "template": (
                "bypass:{contract_execution_id}:revision-"
                "{execution_state_revision}:{stage_id}:{line_id}"
                "[:{line_instance_id}]"
            ),
            "line_specific": True,
            "idempotency_key": True,
        },
        "diagnostic_row_binding": {
            "policy": "new_atomic_or_existing_exact_open",
            "create_new": {
                "recommended": True,
                "request_rule": (
                    "Omit diagnostic_backlog_id. The line-bypass endpoint "
                    "atomically creates and links a new OPEN diagnostic row."
                ),
                "deterministic_id_algorithm": (
                    "AC-CONTRACT-LINE-BYPASS-"
                    "{SHA256(bypass_identity)[0:16].upper()}"
                ),
                "deterministic_id_example": deterministic_diagnostic_id,
                "must_not_precreate_or_reuse": True,
            },
            "reuse_existing_open": {
                "allowed": True,
                "request_rule": (
                    "Set diagnostic_backlog_id only to an existing OPEN bypass "
                    "diagnostic row whose stored binding matches every field "
                    "in required_exact_binding."
                ),
                "required_status": "OPEN",
                "required_exact_binding": exact_binding,
                "all_fields_must_match": True,
                "mismatch_fails_closed": True,
            },
            "generic_root_cause_row": {
                "allowed_as": "evidence_ref_only",
                "allowed_as_diagnostic_backlog_id": False,
                "reason": (
                    "A reusable root-cause row is not the line-specific bypass "
                    "diagnostic binding."
                ),
            },
        },
        "create_new_copy_safe_body": dict(common_body),
        "reuse_existing_open_copy_safe_body": {
            **common_body,
            "diagnostic_backlog_id": (
                "<existing OPEN row with exact source/CEX/line binding>"
            ),
        },
        "invariants": {
            "no_pass_claim": True,
            "written_status": "waived",
            "disposition": "proceeded_with_exception",
            "source_generation_terminal_after_barrier": True,
            "terminal_barrier": [
                "independent_qa",
                "ordered_merge",
                "current_head_full_reconcile_activation",
            ],
            "pre_barrier_forward_integration_required": True,
            "post_barrier_readiness_state": "completed_with_exception",
            "post_barrier_scheduler_eligible": False,
            "post_barrier_current_eligible": False,
            "post_barrier_close_eligible": False,
            "post_barrier_resume_eligible": False,
            "source_backlog_mutated_by_bypass": False,
            "diagnostic_row_remains_open": True,
            "repair_requires_separate_backlog_row": True,
            "fresh_generation_requires_accepted_repair_merge_and_current_head_reconcile": True,
            "strict_line_validation_unchanged": True,
            "strict_revision_and_runtime_guide_hash_validation_unchanged": True,
            "bypass_acceptance_logic_unchanged": True,
        },
    }
    if active_no_pass_generation:
        guide["line_bypass_guidance"]["no_pass_generation"] = dict(
            active_no_pass_generation
        )
        guide["line_bypass_guidance"]["diagnostic_row_binding"] = {
            "policy": "reuse_generation_root_only",
            "create_new": {
                "recommended": False,
                "allowed": False,
                "reason": (
                    "the first bypass already owns the sole diagnostic row "
                    "for this execution generation"
                ),
            },
            "reuse_generation_root": {
                "required": True,
                "diagnostic_backlog_id": str(
                    active_no_pass_generation.get(
                        "root_diagnostic_backlog_id"
                    )
                    or ""
                ),
                "root_bypass_identity": str(
                    active_no_pass_generation.get("root_bypass_identity") or ""
                ),
                "root_line_id": str(
                    active_no_pass_generation.get("root_line_id") or ""
                ),
                "downstream_gate_reason_required": True,
                "new_backlog_row_forbidden": True,
            },
        }
        guide["line_bypass_guidance"]["inherited_bypass_copy_safe_body"] = dict(
            common_body
        )
        guide["line_bypass_guidance"].pop("create_new_copy_safe_body", None)
        guide["line_bypass_guidance"].pop(
            "reuse_existing_open_copy_safe_body", None
        )
        guide["line_bypass_guidance"]["invariants"].update(
            {
                "one_root_diagnostic_per_generation": True,
                "every_inherited_gate_reason_required": True,
                "downstream_missing_upstream_evidence_creates_no_backlog_row": True,
                "old_generation_remains_no_pass_after_repair": True,
                "unlock_starts_fresh_execution_generation": True,
            }
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
