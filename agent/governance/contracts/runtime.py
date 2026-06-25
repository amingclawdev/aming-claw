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
from .guide_compiler import compile_runtime_guide
from .hash import stable_sha256
from .instructions import resolve_instruction_bundle
from .registry import ContractDefinitionRegistry
from .schema import ContractDefinitionError, is_new_execution_allowed
from .write_gate import WriteGateDecision, validate_contract_write


class ContractRuntimeError(ValueError):
    """Raised when a contract execution cannot be started or advanced."""


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
            "instruction_bundle_hash": instruction_bundle["instruction_bundle_hash"],
            "route_token_ref": route_token_ref,
            "completed_lines": [],
            "execution_state_revision": state["execution_state_revision"],
            "execution_state": state,
            "runtime_guide": guide,
            "role_binding": dict(role_binding or {}),
            "backlog_lineage": dict(backlog_lineage or {}),
            "metadata": dict(metadata or {}),
        }
        return self.store.create(record)

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
        record["execution_state"] = state
        record["runtime_guide"] = guide
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
        guide = self.current_guide(
            contract_execution_id,
            actor_role=effective_actor_role,
        )
        refreshed = self.store.get(contract_execution_id)
        decision = validate_contract_write(
            definition,
            refreshed["execution_state"],
            effective_write,
            runtime_guide=guide,
        )
        if not decision.ok:
            return {
                "schema_version": "contract_runtime_write_result.v1",
                "ok": False,
                "decision": decision.to_dict(),
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
            "decision": WriteGateDecision(ok=True).to_dict(),
            "record": self.store.get(contract_execution_id),
        }

    def _load_pinned_definition(self, record: Mapping[str, Any]) -> dict[str, Any]:
        definition = self.registry.get(
            str(record.get("contract_id") or ""),
            version=str(record.get("version") or ""),
            revision=str(record.get("revision") or ""),
            include_deprecated=True,
        )
        _assert_hash("definition_hash", record.get("definition_hash"), definition.get("definition_hash"))
        return definition


def _assert_hash(field: str, expected: Any, actual: Any) -> None:
    if expected != actual:
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
    "payload",
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
