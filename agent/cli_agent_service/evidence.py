"""Public-safe execution receipts for supervised CLI agent runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
_PUBLIC_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9-]{2,127}")
_TICKET_ID_PATTERN = re.compile(r"caet-[0-9a-f]{24}")
_RUNTIME_CONTEXT_ID_PATTERN = re.compile(r"mfrctx-[a-z0-9][a-z0-9-]{2,127}")
_SENSITIVE_IDENTIFIER_TOKEN = re.compile(
    r"(?:^|-)(?:api-?key|credential|password|private|prompt|secret|token)(?:-|$)",
    re.IGNORECASE,
)
_FAILURE_CATEGORIES = frozenset(
    {
        "",
        "cancelled",
        "lease_heartbeat_failed",
        "lost",
        "process_error",
        "receipt_heartbeat_failed",
        "spawn_error",
    }
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
    if not isinstance(value, str):
        raise ValueError("{} must be a string".format(field_name))
    normalized = value.strip()
    if not normalized:
        raise ValueError("{} is required".format(field_name))
    if len(normalized) > 256 or any(ord(char) < 32 for char in normalized):
        raise ValueError("{} contains unsafe text".format(field_name))
    return normalized


def _public_identifier(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if not _PUBLIC_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError("{} must be a public-safe identifier".format(field_name))
    if _SENSITIVE_IDENTIFIER_TOKEN.search(normalized):
        raise ValueError("{} must not contain private-content markers".format(field_name))
    return normalized


def _canonical_timestamp(value: Any) -> str:
    normalized = _required_text(value, "observed_at")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


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
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("process_identity must be an object")
    source = value if isinstance(value, Mapping) else {}
    unknown_fields = set(source) - {
        "pid",
        "process_group_id",
        "process_start_identity",
        "process_start_identity_hash",
    }
    if unknown_fields:
        raise ValueError("process_identity contains unsupported fields")
    normalized: dict[str, Any] = {}
    for field_name in _PROCESS_IDENTITY_FIELDS:
        field_value = source.get(field_name)
        if field_value in (None, ""):
            continue
        if field_name in {"pid", "process_group_id"}:
            if isinstance(field_value, bool) or not isinstance(field_value, int):
                raise ValueError("{} must be an integer".format(field_name))
            integer = field_value
            if integer <= 0:
                raise ValueError("{} must be positive".format(field_name))
            normalized[field_name] = integer
        else:
            raise ValueError(
                "raw process_start_identity is not allowed; provide its content hash"
            )
    process_start_identity_hash = source.get("process_start_identity_hash")
    if process_start_identity_hash not in (None, ""):
        normalized["process_start_identity_hash"] = _content_hash(
            process_start_identity_hash,
            "process_start_identity_hash",
        )
    return normalized


def _metering(value: Mapping[str, Any] | None) -> dict[str, int | float]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("metering must be an object")
    source = value if isinstance(value, Mapping) else {}
    if set(source) - set(_METERING_FIELDS):
        raise ValueError("metering contains unsupported fields")
    normalized: dict[str, int | float] = {}
    for field_name in _METERING_FIELDS:
        field_value = source.get(field_name)
        if field_value in (None, ""):
            continue
        if isinstance(field_value, bool) or not isinstance(field_value, (int, float)):
            raise ValueError("{} must be numeric".format(field_name))
        if field_name == "cost_usd":
            number: int | float = float(field_value)
        else:
            if not isinstance(field_value, int):
                raise ValueError("{} must be an integer".format(field_name))
            number = field_value
        if not math.isfinite(number) or number < 0:
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
        if self.schema_version != RUN_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported CLI agent run receipt schema")
        state = _required_text(self.state, "state").lower()
        if state not in RUN_RECEIPT_STATES:
            raise ValueError("invalid CLI agent run receipt state")
        if isinstance(self.event_index, bool) or not isinstance(self.event_index, int):
            raise ValueError("event_index must be an integer")
        event_index = self.event_index
        if event_index < 0:
            raise ValueError("event_index must not be negative")
        ticket_id = _required_text(self.ticket_id, "ticket_id")
        if not _TICKET_ID_PATTERN.fullmatch(ticket_id):
            raise ValueError("ticket_id must identify a CLI agent execution ticket")
        runtime_context_id = _required_text(
            self.runtime_context_id, "runtime_context_id"
        )
        if not _RUNTIME_CONTEXT_ID_PATTERN.fullmatch(runtime_context_id):
            raise ValueError("runtime_context_id must identify a runtime context")
        process_identity = _process_identity(self.process_identity)
        failure_category = str(self.failure_category or "").strip()
        if self.failure_category is not None and not isinstance(
            self.failure_category, str
        ):
            raise ValueError("failure_category must be a string")
        if failure_category not in _FAILURE_CATEGORIES:
            raise ValueError("failure_category is not public-safe")
        process_required = state in {
            "started",
            "heartbeat",
            "completed",
            "cancelled",
            "lost",
        } or (state == "failed" and failure_category != "spawn_error")
        if process_required and not process_identity.get("process_start_identity_hash"):
            raise ValueError(
                "process_start_identity_hash is required after the process starts"
            )
        if self.output_hash is not None and not isinstance(self.output_hash, str):
            raise ValueError("output_hash must be a string")
        output_hash = (self.output_hash or "").strip()
        if state in RUN_RECEIPT_TERMINAL_STATES:
            output_hash = _content_hash(output_hash, "output_hash")
        elif output_hash:
            output_hash = _content_hash(output_hash, "output_hash")
        duration_ms = self.duration_ms
        if duration_ms is not None:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
                raise ValueError("duration_ms must be an integer")
            if duration_ms < 0:
                raise ValueError("duration_ms must not be negative")
        if state in RUN_RECEIPT_TERMINAL_STATES and duration_ms is None:
            raise ValueError("duration_ms is required for a terminal receipt")
        exit_code = self.exit_code
        if exit_code is not None:
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise ValueError("exit_code must be an integer or null")
        if state not in RUN_RECEIPT_TERMINAL_STATES:
            if output_hash or duration_ms is not None or exit_code is not None or failure_category:
                raise ValueError("non-terminal receipts cannot carry terminal result fields")
        elif state == "completed" and (exit_code != 0 or failure_category):
            raise ValueError("completed receipts require exit_code 0 and no failure")
        elif state == "failed" and (
            exit_code is None or exit_code == 0 or not failure_category
        ):
            raise ValueError("failed receipts require a non-zero exit_code and failure")
        elif state == "cancelled" and (
            exit_code != 130 or failure_category != "cancelled"
        ):
            raise ValueError("cancelled receipts require exit_code 130")
        elif state == "lost" and (
            exit_code is not None or failure_category != "lost"
        ):
            raise ValueError("lost receipts require failure_category lost and no exit_code")
        object.__setattr__(self, "run_id", _public_identifier(self.run_id, "run_id"))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "event_index", event_index)
        object.__setattr__(self, "observed_at", _canonical_timestamp(self.observed_at))
        object.__setattr__(self, "ticket_id", ticket_id)
        object.__setattr__(
            self, "ticket_hash", _content_hash(self.ticket_hash, "ticket_hash")
        )
        object.__setattr__(
            self, "profile_id", _public_identifier(self.profile_id, "profile_id")
        )
        object.__setattr__(self, "runtime_context_id", runtime_context_id)
        object.__setattr__(
            self, "command_hash", _content_hash(self.command_hash, "command_hash")
        )
        object.__setattr__(self, "process_identity", process_identity)
        object.__setattr__(self, "output_hash", output_hash)
        object.__setattr__(self, "duration_ms", duration_ms)
        object.__setattr__(self, "metering", _metering(self.metering))
        object.__setattr__(self, "exit_code", exit_code)
        object.__setattr__(self, "failure_category", failure_category)

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
        allowed_fields = {
            "schema_version",
            "run_id",
            "state",
            "event_index",
            "observed_at",
            "ticket_id",
            "ticket_hash",
            "profile_id",
            "runtime_context_id",
            "command_hash",
            "process_identity",
            "output_hash",
            "duration_ms",
            "metering",
            "exit_code",
            "failure_category",
            "raw_prompt_stored",
            "raw_output_stored",
            "operational_state_only",
            "governance_authority",
            "receipt_id",
            "receipt_hash",
        }
        if set(value) - allowed_fields:
            raise ValueError("CLI agent run receipt contains unsupported fields")
        if value.get("raw_prompt_stored") is not False:
            raise ValueError("raw_prompt_stored must be false")
        if value.get("raw_output_stored") is not False:
            raise ValueError("raw_output_stored must be false")
        if value.get("operational_state_only") is not True:
            raise ValueError("operational_state_only must be true")
        if value.get("governance_authority") is not False:
            raise ValueError("governance_authority must be false")
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
        previous_receipt: Mapping[str, Any] | None = None,
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
        if previous_receipt:
            previous = CliAgentRunReceipt.from_public_dict(previous_receipt)
            for field_name, expected in self._identity.items():
                if getattr(previous, field_name) != expected:
                    raise ValueError(
                        "previous receipt does not match {}".format(field_name)
                    )
            self._next_index = previous.event_index + 1
            self._last_state = previous.state

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
            else {"started", "failed"}
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


class RunReceiptJournal:
    """Durable, append-only local spool for governance receipt projection."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._lock = threading.RLock()

    def _path(self, run_id: str) -> Path:
        safe_run_id = _public_identifier(run_id, "run_id")
        return self.root / "{}.jsonl".format(safe_run_id)

    def receipts(self, run_id: str) -> tuple[dict[str, Any], ...]:
        path = self._path(run_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        receipts = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("run receipt journal contains invalid JSON") from exc
            receipts.append(CliAgentRunReceipt.from_public_dict(value).to_public_dict())
        return tuple(receipts)

    def latest(self, run_id: str) -> dict[str, Any] | None:
        receipts = self.receipts(run_id)
        return receipts[-1] if receipts else None

    def append(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        normalized = CliAgentRunReceipt.from_public_dict(receipt).to_public_dict()
        path = self._path(normalized["run_id"])
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        with self._lock:
            existing = self.receipts(normalized["run_id"])
            for item in existing:
                if item["receipt_id"] == normalized["receipt_id"]:
                    return item
            descriptor = os.open(
                path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return normalized

    __call__ = append


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
