"""Host-private, single-use worker authentication delivery for CLI launches."""

from __future__ import annotations

import math
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


WORKER_SESSION_TOKEN_ENV = "AMING_WORKER_SESSION_TOKEN"
WORKER_FENCE_TOKEN_ENV = "AMING_WORKER_FENCE_TOKEN"
WORKER_AUTH_ENV_KEYS = (WORKER_SESSION_TOKEN_ENV, WORKER_FENCE_TOKEN_ENV)
DEFAULT_HOST_ENVELOPE_TTL_SECONDS = 60.0
MAX_HOST_ENVELOPE_TTL_SECONDS = 3600.0

_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/@+-]{1,511}")
_PUBLIC_FIELDS = (
    "project_id",
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "backlog_id",
    "worker_role",
    "worker_id",
    "worker_slot_id",
    "actual_host_worker_id",
    "worker_session_id",
    "session_token_ref",
)
_RAW_FIELDS = {
    "session_token",
    "fence_token",
    WORKER_SESSION_TOKEN_ENV,
    WORKER_FENCE_TOKEN_ENV,
}


class HostEnvelopeError(ValueError):
    """Invalid host envelope; messages never include supplied values."""


def _wipe(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
    value.clear()


def scrub_host_envelope_payload(value: Any) -> None:
    """Discard known raw envelope fields from mutable request objects."""

    if isinstance(value, MutableMapping):
        for key in tuple(value):
            child = value.get(key)
            if str(key) == "env" and isinstance(child, Mapping):
                scrub_host_envelope_payload(child)
                if isinstance(child, MutableMapping):
                    child.clear()
                value.pop(key, None)
            elif str(key) in _RAW_FIELDS:
                if isinstance(child, bytearray):
                    _wipe(child)
                value.pop(key, None)
            else:
                scrub_host_envelope_payload(child)
    elif isinstance(value, list):
        for child in value:
            scrub_host_envelope_payload(child)


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _SAFE_REF.fullmatch(text):
        raise HostEnvelopeError("host envelope contains an invalid copy-safe reference")
    return text


def _safe_refs(
    envelope: Mapping[str, Any],
    raw_values: tuple[str, str],
) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for field_name in _PUBLIC_FIELDS:
        value = _safe_text(envelope.get(field_name))
        if value:
            refs[field_name] = value
    public_text = "\n".join(refs.values())
    if any(raw_value and raw_value in public_text for raw_value in raw_values):
        raise HostEnvelopeError(
            "host envelope copy-safe references contain raw auth material"
        )
    return refs


def _take_environment(envelope: Mapping[str, Any]) -> dict[str, bytearray]:
    source = envelope.get("env")
    encoded: dict[str, bytearray] = {}
    try:
        if not isinstance(source, Mapping):
            raise HostEnvelopeError("host envelope env must be an object")
        if set(source) != set(WORKER_AUTH_ENV_KEYS):
            raise HostEnvelopeError("host envelope env must contain only worker auth keys")
        for key in WORKER_AUTH_ENV_KEYS:
            value = source.get(key)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise HostEnvelopeError("host envelope worker auth value is invalid")
            encoded[key] = bytearray(value.encode("utf-8"))
        return encoded
    except BaseException:
        for value in encoded.values():
            _wipe(value)
        encoded.clear()
        raise
    finally:
        if isinstance(source, MutableMapping):
            source.clear()
        if isinstance(envelope, MutableMapping):
            envelope.pop("env", None)


def _expiration(
    envelope: Mapping[str, Any],
    *,
    ttl_seconds: Any,
    expires_at: Any,
    now: datetime,
) -> tuple[float, str]:
    ttl_value = ttl_seconds if ttl_seconds not in (None, "") else envelope.get("ttl_seconds")
    ttl: float | None = None
    if ttl_value not in (None, ""):
        try:
            ttl = float(ttl_value)
        except (TypeError, ValueError) as exc:
            raise HostEnvelopeError("host envelope ttl_seconds is invalid") from exc
        if not math.isfinite(ttl) or ttl <= 0:
            raise HostEnvelopeError("host envelope ttl_seconds must be positive")

    expires_value = expires_at if expires_at not in (None, "") else envelope.get("expires_at")
    if expires_value not in (None, ""):
        try:
            absolute = datetime.fromisoformat(str(expires_value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise HostEnvelopeError("host envelope expires_at is invalid") from exc
        absolute = (
            absolute.astimezone(timezone.utc)
            if absolute.tzinfo
            else absolute.replace(tzinfo=timezone.utc)
        )
        remaining = (absolute - now).total_seconds()
        if remaining <= 0:
            raise HostEnvelopeError("host envelope is expired")
        ttl = remaining if ttl is None else min(ttl, remaining)

    effective = min(
        ttl if ttl is not None else DEFAULT_HOST_ENVELOPE_TTL_SECONDS,
        MAX_HOST_ENVELOPE_TTL_SECONDS,
    )
    expires = now + timedelta(seconds=effective)
    return effective, expires.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(repr=False)
class _StoredHostEnvelope:
    run_id: str
    envelope_ref: str
    public_refs: dict[str, Any]
    environment: dict[str, bytearray] = field(repr=False)
    expires_monotonic: float
    expires_at: str

    def summary(self, status: str) -> dict[str, Any]:
        return {
            "schema_version": "cli_agent_service.host_envelope_receipt.v1",
            "status": status,
            "run_id": self.run_id,
            "envelope_ref": self.envelope_ref,
            "expires_at": self.expires_at,
            **self.public_refs,
            "env_keys": list(WORKER_AUTH_ENV_KEYS),
            "session_token_redacted": True,
            "fence_token_redacted": True,
            "single_use": True,
            "raw_worker_auth_exposed": False,
        }

    def wipe(self) -> None:
        for value in self.environment.values():
            _wipe(value)
        self.environment.clear()


class HostEnvelopeDelivery:
    """One consumed delivery whose raw values are never returned to callers."""

    __slots__ = ("_environment", "_applied")

    def __init__(self, environment: dict[str, bytearray]) -> None:
        self._environment = environment
        self._applied = False

    def __repr__(self) -> str:
        return "HostEnvelopeDelivery(redacted=True)"

    def apply_to(self, environment: MutableMapping[str, str]) -> None:
        if self._applied:
            raise HostEnvelopeError("host envelope delivery was already applied")
        applied: list[str] = []
        try:
            for key in WORKER_AUTH_ENV_KEYS:
                secret = self._environment.get(key)
                if secret is None:
                    raise HostEnvelopeError("host envelope delivery is incomplete")
                environment[key] = bytes(secret).decode("utf-8")
                applied.append(key)
        except BaseException:
            for key in applied:
                environment.pop(key, None)
            raise
        self._applied = True

    def discard(self) -> None:
        for value in self._environment.values():
            _wipe(value)
        self._environment.clear()

    def __del__(self) -> None:
        self.discard()


class HostEnvelopeStore:
    """In-memory run-scoped store with no raw read or persistence surface."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, _StoredHostEnvelope] = {}
        self._lock = threading.RLock()

    def _purge_expired_locked(self) -> int:
        now = self._monotonic_clock()
        expired = [
            run_id
            for run_id, entry in self._entries.items()
            if entry.expires_monotonic <= now
        ]
        for run_id in expired:
            self._entries.pop(run_id).wipe()
        return len(expired)

    def stage(
        self,
        run_id: str,
        envelope: Mapping[str, Any],
        *,
        ttl_seconds: Any = None,
        expires_at: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(envelope, Mapping):
            raise HostEnvelopeError("host envelope must be an object")
        environment = _take_environment(envelope)
        try:
            scrub_host_envelope_payload(envelope)
            normalized_run_id = str(run_id or "").strip()
            if not _SAFE_REF.fullmatch(normalized_run_id):
                raise HostEnvelopeError("host envelope run_id is invalid")
            envelope_run_id = str(envelope.get("run_id") or "").strip()
            if envelope_run_id and envelope_run_id != normalized_run_id:
                raise HostEnvelopeError(
                    "host envelope run_id does not match delivery scope"
                )
            raw_values = tuple(
                bytes(environment[key]).decode("utf-8")
                for key in WORKER_AUTH_ENV_KEYS
            )
            public_refs = _safe_refs(envelope, raw_values)
            now = self._wall_clock()
            now = (
                now.astimezone(timezone.utc)
                if now.tzinfo
                else now.replace(tzinfo=timezone.utc)
            )
            ttl, expires = _expiration(
                envelope,
                ttl_seconds=ttl_seconds,
                expires_at=expires_at,
                now=now,
            )
            entry = _StoredHostEnvelope(
                run_id=normalized_run_id,
                envelope_ref="clihe-{}".format(uuid.uuid4().hex),
                public_refs=public_refs,
                environment=environment,
                expires_monotonic=self._monotonic_clock() + ttl,
                expires_at=expires,
            )
        except BaseException:
            for value in environment.values():
                _wipe(value)
            environment.clear()
            raise

        with self._lock:
            self._purge_expired_locked()
            previous = self._entries.pop(normalized_run_id, None)
            if previous is not None:
                previous.wipe()
            self._entries[normalized_run_id] = entry
        return entry.summary("staged")

    def consume(self, run_id: str) -> HostEnvelopeDelivery | None:
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.pop(str(run_id or "").strip(), None)
        if entry is None:
            return None
        environment = entry.environment
        entry.environment = {}
        return HostEnvelopeDelivery(environment)

    def revoke(self, run_id: str, *, envelope_ref: str = "") -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(normalized_run_id)
            if entry is None:
                return {
                    "schema_version": "cli_agent_service.host_envelope_receipt.v1",
                    "status": "not_found",
                    "run_id": normalized_run_id,
                    "single_use": True,
                    "raw_worker_auth_exposed": False,
                }
            requested_ref = str(envelope_ref or "").strip()
            if requested_ref and requested_ref != entry.envelope_ref:
                raise HostEnvelopeError("host envelope ref does not match delivery scope")
            self._entries.pop(normalized_run_id)
        summary = entry.summary("revoked")
        entry.wipe()
        return summary

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired_locked()

    def revoke_all(self) -> None:
        with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.wipe()

    def pending_count(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._entries)


_DEFAULT_HOST_ENVELOPE_STORE = HostEnvelopeStore()


def default_host_envelope_store() -> HostEnvelopeStore:
    return _DEFAULT_HOST_ENVELOPE_STORE


def child_process_environment(
    base_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    """Build an explicit child env that cannot inherit worker auth accidentally."""

    environment = dict(os.environ if base_environment is None else base_environment)
    for key in WORKER_AUTH_ENV_KEYS:
        environment.pop(key, None)
        if isinstance(base_environment, MutableMapping):
            base_environment.pop(key, None)
    return environment


def clear_worker_auth_environment(environment: MutableMapping[str, str]) -> None:
    for key in WORKER_AUTH_ENV_KEYS:
        environment.pop(key, None)
