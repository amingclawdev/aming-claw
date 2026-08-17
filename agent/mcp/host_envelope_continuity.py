"""Process-local worker host-envelope continuity for non-CLI MCP hosts.

Raw worker authentication is staged in ``HostEnvelopeStore`` and is never
returned from this layer.  A later worker Guide/read-receipt/startup call may
borrow the exact identity-bound envelope inside the same MCP process.  The
successful startup response is the consumption acknowledgement and zeroizes
the staged credentials.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

try:
    from agent.cli_agent_service.launchers import (
        HostEnvelopeError,
        HostEnvelopeStore,
        scrub_host_envelope_payload,
    )
except ModuleNotFoundError:  # ``agent`` directory is the direct script root.
    from cli_agent_service.launchers import (
        HostEnvelopeError,
        HostEnvelopeStore,
        scrub_host_envelope_payload,
    )


ISSUANCE_TOOLS = frozenset(
    {
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_reissue",
        "runtime_context_session_token_rejoin",
    }
)
CONTINUATION_TOOLS = frozenset(
    {
        "runtime_context_worker_guide",
        "runtime_context_read_receipt",
        "parallel_branch_startup",
    }
)
MANAGED_TOOLS = ISSUANCE_TOOLS | CONTINUATION_TOOLS

_ROUTE_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
)
_BASE_BINDING_FIELDS = (
    "project_id",
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "contract_execution_id",
    "target_project_root",
    "session_token_ref",
    *_ROUTE_FIELDS,
)
_WORKER_BINDING_FIELDS = (
    "worker_id",
    "worker_slot_id",
    "agent_id",
    "allocation_owner",
    "actual_host_worker_id",
    "worker_session_id",
    "host_startup_id",
    "host_session_id",
)
_RAW_RESPONSE_FIELDS = ("session_token", "fence_token")
_OWNER_ID = "mcp-host-envelope-continuity"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _route_identity(value: Mapping[str, Any]) -> dict[str, str]:
    nested = value.get("route_identity")
    nested = nested if isinstance(nested, Mapping) else {}
    return {
        field: _text(value.get(field) or nested.get(field))
        for field in _ROUTE_FIELDS
        if _text(value.get(field) or nested.get(field))
    }


def _local_error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": code,
        "message": message,
        "source": "mcp_host_envelope_continuity",
        "zero_write_rejection": True,
        "writes_performed": False,
        "http_request_performed": False,
        "raw_worker_auth_exposed": False,
        **details,
    }


def _run_id(binding: Mapping[str, str]) -> str:
    core = {
        field: _text(binding.get(field))
        for field in ("project_id", "runtime_context_id", "task_id")
    }
    digest = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"mcphe-{digest}"


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _ManagedEnvelope:
    run_id: str
    envelope_ref: str
    binding: dict[str, str]


class ManagedHostEnvelopeContinuity:
    """One MCP-process-local managed worker credential continuation."""

    def __init__(self, *, store: HostEnvelopeStore | None = None) -> None:
        self._store = store or HostEnvelopeStore()
        self._entries: dict[str, _ManagedEnvelope] = {}
        self._in_flight: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def handles(tool_name: str) -> bool:
        return tool_name in MANAGED_TOOLS

    def pending_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def _capture_issuance(
        self,
        args: Mapping[str, Any],
        result: Any,
    ) -> Any:
        if not isinstance(result, dict):
            return result
        if result.get("ok") is not True:
            scrub_host_envelope_payload(result)
            return result
        host_envelope = result.get("host_envelope")
        if not isinstance(host_envelope, dict):
            scrub_host_envelope_payload(result)
            # Preserve compatibility with mocked/legacy host facades that do
            # not claim delivery.  A real ``delivery=worker_host_envelope``
            # response must carry the typed envelope and fails closed below.
            if _text(result.get("delivery")) != "worker_host_envelope":
                return result
            return _local_error(
                "managed_host_envelope_missing",
                "Successful host authentication did not return a typed host envelope.",
            )
        environment = host_envelope.get("env")
        if not isinstance(environment, Mapping):
            scrub_host_envelope_payload(result)
            return _local_error(
                "managed_host_envelope_incomplete",
                "Successful host authentication returned no process-local auth env.",
            )
        raw_session = _text(environment.get("AMING_WORKER_SESSION_TOKEN"))
        raw_fence = _text(environment.get("AMING_WORKER_FENCE_TOKEN"))
        top_session = _text(result.get("session_token"))
        top_fence = _text(result.get("fence_token"))
        if not raw_session or not raw_fence or (
            top_session and top_session != raw_session
        ) or (top_fence and top_fence != raw_fence):
            scrub_host_envelope_payload(result)
            return _local_error(
                "managed_host_envelope_auth_mismatch",
                "Host envelope auth does not match the successful response.",
            )
        for field, raw_value in (
            ("session_token_hash", raw_session),
            ("fence_token_hash", raw_fence),
        ):
            verifier = _text(result.get(field) or host_envelope.get(field))
            if verifier and verifier != _sha256(raw_value):
                scrub_host_envelope_payload(result)
                return _local_error(
                    "managed_host_envelope_verifier_mismatch",
                    "Host envelope verifier does not match the delivered credential.",
                    field=field,
                )

        route = {
            **_route_identity(args),
            **_route_identity(result),
            **_route_identity(host_envelope),
        }
        binding: dict[str, str] = {}
        for field in (*_BASE_BINDING_FIELDS, *_WORKER_BINDING_FIELDS):
            values = {
                _text(source.get(field))
                for source in (args, result, host_envelope)
                if _text(source.get(field))
            }
            if field in _ROUTE_FIELDS and route.get(field):
                values.add(route[field])
            if len(values) > 1:
                scrub_host_envelope_payload(result)
                return _local_error(
                    "managed_host_envelope_identity_mismatch",
                    "Host envelope identity does not match the request/response scope.",
                    field=field,
                )
            if values:
                binding[field] = values.pop()
        required = (
            "project_id",
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "session_token_ref",
            *_ROUTE_FIELDS,
        )
        missing = [field for field in required if not binding.get(field)]
        if missing:
            scrub_host_envelope_payload(result)
            return _local_error(
                "managed_host_envelope_identity_incomplete",
                "Host envelope is missing required copy-safe identity.",
                missing_fields=missing,
            )

        staged_envelope = {
            **host_envelope,
            **binding,
            "env": environment,
        }
        run_id = _run_id(binding)
        try:
            receipt = self._store.stage(
                run_id,
                staged_envelope,
                lease_owner_id=_OWNER_ID,
                ttl_seconds=result.get("ttl_seconds"),
                expires_at=result.get("expires_at"),
            )
        except HostEnvelopeError:
            scrub_host_envelope_payload(result)
            return _local_error(
                "managed_host_envelope_stage_rejected",
                "Host envelope could not be staged in this MCP process.",
            )
        entry = _ManagedEnvelope(
            run_id=run_id,
            envelope_ref=_text(receipt.get("envelope_ref")),
            binding=binding,
        )
        with self._lock:
            self._entries[run_id] = entry
        scrub_host_envelope_payload(result)
        result.pop("host_envelope", None)
        result["auth_loaded"] = True
        result["managed_host_envelope"] = {
            "schema_version": "mcp.managed_host_envelope.v1",
            "status": "staged",
            "session_token_ref": binding["session_token_ref"],
            "runtime_context_id": binding["runtime_context_id"],
            "task_id": binding["task_id"],
            "raw_worker_auth_exposed": False,
            "process_local": True,
            "startup_consumption_required": True,
        }
        return result

    def _entry_for(self, args: Mapping[str, Any]) -> _ManagedEnvelope | None:
        candidate_binding = {
            field: _text(args.get(field))
            for field in ("project_id", "runtime_context_id", "task_id")
        }
        if not all(candidate_binding.values()):
            return None
        with self._lock:
            return self._entries.get(_run_id(candidate_binding))

    def _validate_continuation(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        entry: _ManagedEnvelope,
    ) -> dict[str, Any] | None:
        if any(_text(args.get(field)) for field in _RAW_RESPONSE_FIELDS):
            return _local_error(
                "managed_host_envelope_auth_ambiguous",
                "Do not combine managed process-local auth with caller-provided raw auth.",
            )
        required = [
            "project_id",
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "target_project_root",
            "session_token_ref",
            *_ROUTE_FIELDS,
        ]
        if tool_name == "runtime_context_read_receipt":
            required.extend(
                ("contract_execution_id", "worker_id", "worker_slot_id")
            )
        elif tool_name == "parallel_branch_startup":
            required.extend(
                (
                    "contract_execution_id",
                    "actual_host_worker_id",
                    "worker_session_id",
                )
            )
        mismatches: list[str] = []
        missing: list[str] = []
        supplied_route = _route_identity(args)
        for field in (*_BASE_BINDING_FIELDS, *_WORKER_BINDING_FIELDS):
            expected = _text(entry.binding.get(field))
            if not expected:
                continue
            actual = _text(args.get(field))
            if field in _ROUTE_FIELDS:
                actual = _text(args.get(field) or supplied_route.get(field))
            if not actual and field in required:
                missing.append(field)
            elif actual and actual != expected:
                mismatches.append(field)
        if missing or mismatches:
            return _local_error(
                "managed_host_envelope_scope_mismatch",
                "Managed host envelope scope does not match this continuation.",
                missing_fields=sorted(set(missing)),
                mismatched_fields=sorted(set(mismatches)),
            )
        return None

    def dispatch(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> Any:
        request_args = dict(args)
        if tool_name in ISSUANCE_TOOLS:
            return self._capture_issuance(request_args, send(request_args))

        entry = self._entry_for(request_args)
        if entry is None:
            if (
                tool_name == "runtime_context_worker_guide"
                or not _text(request_args.get("session_token_ref"))
            ):
                return send(request_args)
            return _local_error(
                "managed_host_envelope_not_loaded",
                "This MCP process has no exact staged worker host envelope.",
            )
        rejection = self._validate_continuation(tool_name, request_args, entry)
        if rejection is not None:
            return rejection

        with self._lock:
            if entry.run_id in self._in_flight:
                return _local_error(
                    "managed_host_envelope_concurrent_use",
                    "The exact process-local host envelope already has an in-flight continuation.",
                )
            self._in_flight.add(entry.run_id)

        try:
            delivery = self._store.borrow(
                entry.run_id,
                lease_owner_id=_OWNER_ID,
                envelope_ref=entry.envelope_ref,
                expected_public_refs=entry.binding,
            )
            if delivery is None:
                with self._lock:
                    self._entries.pop(entry.run_id, None)
                return _local_error(
                    "managed_host_envelope_unavailable",
                    "The process-local worker host envelope is absent or expired.",
                )
            enriched = dict(request_args)
            temporary_environment: dict[str, str] = {}
            try:
                delivery.apply_to(temporary_environment)
                enriched["session_token"] = temporary_environment[
                    "AMING_WORKER_SESSION_TOKEN"
                ]
                enriched["fence_token"] = temporary_environment[
                    "AMING_WORKER_FENCE_TOKEN"
                ]
                result = send(enriched)
            finally:
                enriched.pop("session_token", None)
                enriched.pop("fence_token", None)
                temporary_environment.clear()
                delivery.discard()
        finally:
            with self._lock:
                self._in_flight.discard(entry.run_id)

        scrub_host_envelope_payload(result)
        if (
            tool_name == "parallel_branch_startup"
            and isinstance(result, dict)
            and result.get("ok") is True
            and _text(result.get("status"))
            in {"accepted", "started", "startup_recorded", "running"}
        ):
            self._store.acknowledge(
                entry.run_id,
                lease_owner_id=_OWNER_ID,
                envelope_ref=entry.envelope_ref,
                expected_public_refs=entry.binding,
            )
            with self._lock:
                self._entries.pop(entry.run_id, None)
            result["managed_host_envelope_consumed"] = True
            result["raw_worker_auth_exposed"] = False
        return result


_DEFAULT_CONTINUITY = ManagedHostEnvelopeContinuity()


def default_managed_host_envelope_continuity() -> ManagedHostEnvelopeContinuity:
    return _DEFAULT_CONTINUITY
