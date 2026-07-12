"""Public-safe execution receipts for supervised CLI agent runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


EXECUTION_RECEIPT_SCHEMA_VERSION = "cli_agent_service.execution_receipt.v1"
RUN_RECEIPT_SCHEMA_VERSION = "cli_agent_service.run_receipt.v1"
RUN_RECEIPT_STATES = (
    "accepted",
    "started",
    "heartbeat",
    "completed",
    "failed",
    "cancelled",
    "lost",
)
RUN_RECEIPT_TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "lost"}
)
_HASH_PREFIX = "sha256:"
_PROCESS_IDENTITY_FIELDS = (
    "pid",
    "process_group_id",
    "process_start_identity",
)
_METERING_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cost_usd",
)


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def hash_text(value: str) -> str:
    return sha256_bytes(str(value).encode("utf-8"))


def hash_command(command: Sequence[str]) -> str:
    payload = json.dumps(list(command), separators=(",", ":"), ensure_ascii=True)
    return hash_text(payload)


def hash_file(path: str | Path) -> str:
    try:
        value = Path(path).read_bytes()
    except OSError:
        value = b""
    return sha256_bytes(value)


def _required_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("{} is required".format(field_name))
    return normalized


def _content_hash(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if (
        not normalized.startswith(_HASH_PREFIX)
        or len(normalized) != len(_HASH_PREFIX) + 64
        or any(char not in "0123456789abcdef" for char in normalized[len(_HASH_PREFIX) :])
    ):
        raise ValueError("{} must be a sha256 content hash".format(field_name))
    return normalized


def _process_identity(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    normalized: dict[str, Any] = {}
    for field_name in _PROCESS_IDENTITY_FIELDS:
        field_value = source.get(field_name)
        if field_value in (None, ""):
            continue
        if field_name in {"pid", "process_group_id"}:
            integer = int(field_value)
            if integer <= 0:
                raise ValueError("{} must be positive".format(field_name))
            normalized[field_name] = integer
        else:
            normalized[field_name] = _required_text(field_value, field_name)
    return normalized


def _metering(value: Mapping[str, Any] | None) -> dict[str, int | float]:
    source = value if isinstance(value, Mapping) else {}
    normalized: dict[str, int | float] = {}
    for field_name in _METERING_FIELDS:
        field_value = source.get(field_name)
        if field_value in (None, ""):
            continue
        if field_name == "cost_usd":
            number: int | float = float(field_value)
        else:
            number = int(field_value)
        if number < 0:
            raise ValueError("{} must not be negative".format(field_name))
        normalized[field_name] = number
    return normalized


@dataclass(frozen=True)
class CliAgentRunReceipt:
    """One public-safe, idempotent CLI run lifecycle fact."""

    run_id: str
    state: str
    event_index: int
    observed_at: str
    ticket_id: str
    ticket_hash: str
    profile_id: str
    runtime_context_id: str
    command_hash: str
    process_identity: Mapping[str, Any] = field(default_factory=dict)
    output_hash: str = ""
    duration_ms: int | None = None
    metering: Mapping[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    failure_category: str = ""
    schema_version: str = RUN_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        state = str(self.state or "").strip().lower()
        if state not in RUN_RECEIPT_STATES:
            raise ValueError("invalid CLI agent run receipt state")
        event_index = int(self.event_index)
        if event_index < 0:
            raise ValueError("event_index must not be negative")
        ticket_id = _required_text(self.ticket_id, "ticket_id")
        if not ticket_id.startswith("caet-"):
            raise ValueError("ticket_id must identify a CLI agent execution ticket")
        runtime_context_id = _required_text(
            self.runtime_context_id, "runtime_context_id"
        )
        if not runtime_context_id.startswith("mfrctx-"):
            raise ValueError("runtime_context_id must identify a runtime context")
        process_identity = _process_identity(self.process_identity)
        if state != "accepted" and not process_identity.get("process_start_identity"):
            raise ValueError(
                "process_start_identity is required after the run is accepted"
            )
        output_hash = str(self.output_hash or "").strip()
        if state in RUN_RECEIPT_TERMINAL_STATES:
            output_hash = _content_hash(output_hash, "output_hash")
        elif output_hash:
            output_hash = _content_hash(output_hash, "output_hash")
        duration_ms = self.duration_ms
        if duration_ms is not None:
            duration_ms = int(duration_ms)
            if duration_ms < 0:
                raise ValueError("duration_ms must not be negative")
        if state in RUN_RECEIPT_TERMINAL_STATES and duration_ms is None:
            raise ValueError("duration_ms is required for a terminal receipt")
        object.__setattr__(self, "run_id", _required_text(self.run_id, "run_id"))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "event_index", event_index)
        object.__setattr__(
            self, "observed_at", _required_text(self.observed_at, "observed_at")
        )
        object.__setattr__(self, "ticket_id", ticket_id)
        object.__setattr__(
            self, "ticket_hash", _content_hash(self.ticket_hash, "ticket_hash")
        )
        object.__setattr__(
            self, "profile_id", _required_text(self.profile_id, "profile_id")
        )
        object.__setattr__(self, "runtime_context_id", runtime_context_id)
        object.__setattr__(
            self, "command_hash", _content_hash(self.command_hash, "command_hash")
        )
        object.__setattr__(self, "process_identity", process_identity)
        object.__setattr__(self, "output_hash", output_hash)
        object.__setattr__(self, "duration_ms", duration_ms)
        object.__setattr__(self, "metering", _metering(self.metering))
        object.__setattr__(self, "failure_category", str(self.failure_category or "").strip())

    def _material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "event_index": self.event_index,
            "observed_at": self.observed_at,
            "ticket_id": self.ticket_id,
            "ticket_hash": self.ticket_hash,
            "profile_id": self.profile_id,
            "runtime_context_id": self.runtime_context_id,
            "command_hash": self.command_hash,
            "process_identity": dict(self.process_identity),
            "output_hash": self.output_hash,
            "duration_ms": self.duration_ms,
            "metering": dict(self.metering),
            "exit_code": self.exit_code,
            "failure_category": self.failure_category,
            "raw_prompt_stored": False,
            "raw_output_stored": False,
            "operational_state_only": True,
            "governance_authority": False,
        }

    @property
    def receipt_hash(self) -> str:
        encoded = json.dumps(
            self._material(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return sha256_bytes(encoded)

    @property
    def receipt_id(self) -> str:
        return "clirct-" + self.receipt_hash.removeprefix(_HASH_PREFIX)[:24]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            **self._material(),
            "receipt_id": self.receipt_id,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_public_dict(cls, value: Mapping[str, Any]) -> "CliAgentRunReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("CLI agent run receipt must be an object")
        receipt = cls(
            run_id=value.get("run_id", ""),
            state=value.get("state", ""),
            event_index=value.get("event_index", -1),
            observed_at=value.get("observed_at", ""),
            ticket_id=value.get("ticket_id", ""),
            ticket_hash=value.get("ticket_hash", ""),
            profile_id=value.get("profile_id", ""),
            runtime_context_id=value.get("runtime_context_id", ""),
            command_hash=value.get("command_hash", ""),
            process_identity=value.get("process_identity", {}),
            output_hash=value.get("output_hash", ""),
            duration_ms=value.get("duration_ms"),
            metering=value.get("metering", {}),
            exit_code=value.get("exit_code"),
            failure_category=value.get("failure_category", ""),
            schema_version=value.get("schema_version", RUN_RECEIPT_SCHEMA_VERSION),
        )
        if receipt.schema_version != RUN_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported CLI agent run receipt schema")
        if value.get("receipt_id") != receipt.receipt_id:
            raise ValueError("CLI agent run receipt id does not match its content")
        if value.get("receipt_hash") != receipt.receipt_hash:
            raise ValueError("CLI agent run receipt hash does not match its content")
        return receipt


class RunReceiptEmitter:
    """Emit one contiguous lifecycle for a run without retaining raw content."""

    def __init__(
        self,
        *,
        run_id: str,
        ticket_id: str,
        ticket_hash: str,
        profile_id: str,
        runtime_context_id: str,
        command_hash: str,
        sink: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._identity = {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "ticket_hash": ticket_hash,
            "profile_id": profile_id,
            "runtime_context_id": runtime_context_id,
            "command_hash": command_hash,
        }
        self._sink = sink
        self._next_index = 0
        self._last_state = ""

    def emit(
        self,
        state: str,
        *,
        observed_at: str,
        process_identity: Mapping[str, Any] | None = None,
        output_hash: str = "",
        duration_ms: int | None = None,
        metering: Mapping[str, Any] | None = None,
        exit_code: int | None = None,
        failure_category: str = "",
    ) -> CliAgentRunReceipt:
        normalized_state = str(state or "").strip().lower()
        if self._last_state in RUN_RECEIPT_TERMINAL_STATES:
            raise ValueError("a terminal run receipt has already been emitted")
        allowed = (
            {"accepted"}
            if not self._last_state
            else {"started"}
            if self._last_state == "accepted"
            else {"heartbeat", *RUN_RECEIPT_TERMINAL_STATES}
        )
        if normalized_state not in allowed:
            raise ValueError("CLI agent run receipt state is out of order")
        receipt = CliAgentRunReceipt(
            **self._identity,
            state=normalized_state,
            event_index=self._next_index,
            observed_at=observed_at,
            process_identity=process_identity or {},
            output_hash=output_hash,
            duration_ms=duration_ms,
            metering=metering or {},
            exit_code=exit_code,
            failure_category=failure_category,
        )
        self._sink(receipt.to_public_dict())
        self._last_state = normalized_state
        self._next_index += 1
        return receipt


@dataclass(frozen=True)
class ExecutionReceipt:
    run_id: str
    status: str
    exit_code: int
    pid: int
    process_group_id: int
    command_hash: str
    prompt_hash: str
    output_hash: str
    stdout_hash: str
    stderr_hash: str
    started_at: str
    finished_at: str
    failure_category: str = ""
    schema_version: str = EXECUTION_RECEIPT_SCHEMA_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "command_hash": self.command_hash,
            "prompt_hash": self.prompt_hash,
            "output_hash": self.output_hash,
            "stdout_hash": self.stdout_hash,
            "stderr_hash": self.stderr_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_category": self.failure_category,
            "raw_prompt_stored": False,
            "raw_output_stored": False,
            "operational_state_only": True,
            "governance_authority": False,
        }
