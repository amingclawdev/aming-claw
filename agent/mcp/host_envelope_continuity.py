"""Process-local worker host-envelope continuity for non-CLI MCP hosts.

Raw worker authentication from allocation or host issuance is staged in
``HostEnvelopeStore`` and is never returned from this layer.  A later worker
Guide/read-receipt/startup call may borrow the exact identity-bound envelope
inside the same MCP process.  The successful startup response is the
consumption acknowledgement and zeroizes the staged credentials.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
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
ALLOCATION_TOOLS = frozenset({"parallel_branch_allocate"})
CONTINUATION_TOOLS = frozenset(
    {
        "graph_query",
        "runtime_context_current",
        "runtime_context_worker_guide",
        "runtime_context_read_receipt",
        "contract_runtime_submit_line",
        "parallel_branch_startup",
        "runtime_context_implementation_evidence",
        "runtime_context_worker_commit",
        "runtime_context_finish_time_worker_attestation",
        "runtime_context_finish_gate",
        "runtime_context_scope_insufficiency_request",
    }
)
MANAGED_TOOLS = ALLOCATION_TOOLS | ISSUANCE_TOOLS | CONTINUATION_TOOLS

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
_MANAGED_FENCE_ENV_REF = "env:AMING_WORKER_FENCE_TOKEN"
_OWNER_ID = "mcp-host-envelope-continuity"
_POST_RESPONSE_SAFE_FIELDS = (
    "request_id",
    "audit_event_ref",
    "audit_event_id",
    "status",
    "delivery",
    "project_id",
    "backlog_id",
    "session_token_ref",
    "managed_host_envelope_ref",
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "contract_execution_id",
    "target_project_root",
    "worker_id",
    "worker_slot_id",
    *_ROUTE_FIELDS,
    "expires_at",
    "ttl_seconds",
)

_PUBLIC_AUTH_FLAG_FIELDS = (
    "raw_worker_auth_exposed",
    "raw_session_token_exposed",
    "raw_fence_token_exposed",
)
_ALLOCATION_AUTH_CONTAINER_FIELDS = frozenset(
    {
        "auth",
        "credentials",
        "credential_bundle",
        "host_envelope",
        "same_owner_worker_session",
        "secrets",
        "worker_auth",
        "worker_credentials",
    }
)
_ALLOCATION_AUTH_HASH_FIELDS = frozenset(
    {
        "fence_token_hash",
        "session_token_hash",
        "token_hash",
        "token_verifier",
        "worker_auth_hash",
    }
)


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


def _post_response_error(
    result: dict[str, Any],
    code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    """Return a secret-free error without erasing a successful HTTP write.

    Host capture runs after the protected facade returns.  When that facade
    accepted a credential rotation, a local staging failure must not be
    described as a zero-write rejection: doing so hides the new current safe
    ref and makes the next recovery attempt stale before it begins.
    """

    safe_result = {
        field: result.get(field)
        for field in _POST_RESPONSE_SAFE_FIELDS
        if result.get(field) not in (None, "")
    }
    scrub_host_envelope_payload(result)
    result.pop("host_envelope", None)
    return {
        **safe_result,
        "ok": False,
        "error": code,
        "message": message,
        "source": "mcp_host_envelope_continuity",
        "zero_write_rejection": False,
        "writes_performed": True,
        "http_request_performed": True,
        "server_mutation_accepted": True,
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


def _scrub_allocation_auth_payload(
    value: Any,
    *,
    raw_values: tuple[str, ...] = (),
) -> None:
    """Recursively remove allocation auth from a model-visible response.

    The allocation facade historically nested its same-owner bearer under a
    benign-looking response object and also returned the allocation fence in
    ``context``.  Key-based removal alone is therefore insufficient: known
    credential containers and any scalar carrying one of the exact delivered
    raw values are removed as well.  Copy-safe refs remain public.
    """

    secrets = tuple(item for item in raw_values if item)
    if isinstance(value, dict):
        for key in tuple(value):
            normalized = str(key).strip().lower()
            child = value.get(key)
            if normalized.startswith("raw_") and normalized.endswith("_exposed"):
                value[key] = False
                continue
            if (
                normalized in _ALLOCATION_AUTH_CONTAINER_FIELDS
                or normalized in _ALLOCATION_AUTH_HASH_FIELDS
                or normalized in _RAW_RESPONSE_FIELDS
                or normalized in {
                    "aming_worker_session_token",
                    "aming_worker_fence_token",
                }
                or (
                    normalized.startswith("raw_")
                    and ("token" in normalized or "auth" in normalized)
                )
                or normalized.endswith("_token")
                or normalized.endswith("_token_hash")
                or normalized.endswith("_token_verifier")
                or normalized.endswith("_credentials")
                or normalized.endswith("_worker_auth")
            ):
                scrub_host_envelope_payload(child)
                value.pop(key, None)
                continue
            if isinstance(child, str) and any(secret in child for secret in secrets):
                value.pop(key, None)
                continue
            _scrub_allocation_auth_payload(child, raw_values=secrets)
    elif isinstance(value, list):
        kept: list[Any] = []
        for child in value:
            if isinstance(child, str) and any(secret in child for secret in secrets):
                continue
            _scrub_allocation_auth_payload(child, raw_values=secrets)
            kept.append(child)
        value[:] = kept


@dataclass(frozen=True)
class _ManagedEnvelope:
    run_id: str
    envelope_ref: str
    binding: dict[str, str]
    fence_token_hash: str
    expired: bool = False


class ManagedHostEnvelopeContinuity:
    """One MCP-process-local managed worker credential continuation."""

    def __init__(self, *, store: HostEnvelopeStore | None = None) -> None:
        self._store = store or HostEnvelopeStore()
        self._entries: dict[str, _ManagedEnvelope] = {}
        self._revoked_envelope_refs: set[str] = set()
        self._in_flight: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def handles(tool_name: str) -> bool:
        return tool_name in MANAGED_TOOLS

    def pending_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def has_staged_entry(self, args: Mapping[str, Any]) -> bool:
        """Return whether this exact public worker identity is staged locally."""

        return self._entry_for(args) is not None

    def _remember_revoked_ref(self, envelope_ref: str) -> None:
        normalized = _text(envelope_ref)
        if not normalized:
            return
        with self._lock:
            self._revoked_envelope_refs.add(normalized)
            while len(self._revoked_envelope_refs) > 1024:
                self._revoked_envelope_refs.pop()

    def _is_revoked_ref(self, envelope_ref: str) -> bool:
        normalized = _text(envelope_ref)
        if not normalized:
            return False
        with self._lock:
            return normalized in self._revoked_envelope_refs

    @staticmethod
    def _preflight_issuance(
        tool_name: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Validate immutable caller identity before an issuance HTTP call."""

        required = ("project_id", "runtime_context_id", "task_id")
        missing = [field for field in required if not _text(args.get(field))]
        if missing:
            return _local_error(
                "managed_host_envelope_identity_incomplete",
                "Host-envelope issuance is missing immutable request identity.",
                missing_fields=missing,
            )
        alias_groups = {
            "target_project_root": (
                args.get("target_project_root"),
                args.get("project_root"),
                args.get("repo_root"),
            ),
            "actual_host_worker_id": (
                args.get("actual_host_worker_id"),
                args.get("host_worker_id"),
            ),
        }
        nested_route = args.get("route_identity")
        nested_route = nested_route if isinstance(nested_route, Mapping) else {}
        for field in _ROUTE_FIELDS:
            alias_groups[field] = (args.get(field), nested_route.get(field))
        conflicting = [
            field
            for field, candidates in alias_groups.items()
            if len({_text(value) for value in candidates if _text(value)}) > 1
        ]
        if conflicting:
            return _local_error(
                "managed_host_envelope_identity_ambiguous",
                "Host-envelope issuance contains conflicting immutable identity.",
                mismatched_fields=sorted(conflicting),
            )
        if (
            tool_name == "runtime_context_session_token_reissue"
            and bool(_text(args.get("session_token")))
            != bool(_text(args.get("fence_token")))
        ):
            return _local_error(
                "managed_host_envelope_auth_ambiguous",
                "Session-token reissue must use one complete authentication branch.",
            )
        return None

    def _capture_issuance(
        self,
        args: Mapping[str, Any],
        result: Any,
    ) -> Any:
        if not isinstance(result, dict):
            return result
        # HTTP adapters and tests may retain the request/result object for
        # audit.  Public redaction must never mutate that transport record or
        # any request body it aliases.
        result = copy.deepcopy(result)
        if result.get("ok") is not True:
            scrub_host_envelope_payload(result)
            return result
        host_envelope = result.get("host_envelope")
        if not isinstance(host_envelope, dict):
            # Preserve compatibility with mocked/legacy host facades that do
            # not claim delivery.  A real ``delivery=worker_host_envelope``
            # response must carry the typed envelope and fails closed below.
            if _text(result.get("delivery")) != "worker_host_envelope":
                scrub_host_envelope_payload(result)
                return result
            return _post_response_error(
                result,
                "managed_host_envelope_missing",
                "Successful host authentication did not return a typed host envelope.",
            )
        environment = host_envelope.get("env")
        if not isinstance(environment, Mapping):
            return _post_response_error(
                result,
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
            return _post_response_error(
                result,
                "managed_host_envelope_auth_mismatch",
                "Host envelope auth does not match the successful response.",
            )
        for field, raw_value in (
            ("session_token_hash", raw_session),
            ("fence_token_hash", raw_fence),
        ):
            verifier = _text(result.get(field) or host_envelope.get(field))
            if verifier and verifier != _sha256(raw_value):
                return _post_response_error(
                    result,
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
        for field in (
            *(
                candidate
                for candidate in _BASE_BINDING_FIELDS
                if candidate != "session_token_ref"
            ),
            *_WORKER_BINDING_FIELDS,
        ):
            values = {
                _text(source.get(field))
                for source in (args, result, host_envelope)
                if _text(source.get(field))
            }
            if field in _ROUTE_FIELDS and route.get(field):
                values.add(route[field])
            if len(values) > 1:
                return _post_response_error(
                    result,
                    "managed_host_envelope_identity_mismatch",
                    "Host envelope identity does not match the request/response scope.",
                    field=field,
                )
            if values:
                binding[field] = values.pop()
        response_refs = {
            _text(source.get("session_token_ref"))
            for source in (result, host_envelope)
            if _text(source.get("session_token_ref"))
        }
        if len(response_refs) != 1:
            return _post_response_error(
                result,
                "managed_host_envelope_identity_mismatch",
                "Host envelope response did not identify one exact current safe ref.",
                field="session_token_ref",
            )
        binding["session_token_ref"] = response_refs.pop()
        # Keep only a verifier for compatibility with managed clients that
        # still echo the allocation fence on continuation calls.  The raw
        # fence remains process-local in HostEnvelopeStore.
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
            return _post_response_error(
                result,
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
            return _post_response_error(
                result,
                "managed_host_envelope_stage_rejected",
                "Host envelope could not be staged in this MCP process.",
            )
        entry = _ManagedEnvelope(
            run_id=run_id,
            envelope_ref=_text(receipt.get("envelope_ref")),
            binding=binding,
            fence_token_hash=_sha256(raw_fence),
        )
        if not entry.envelope_ref:
            return _post_response_error(
                result,
                "managed_host_envelope_ref_missing",
                "Host envelope staging returned no process-local opaque ref.",
            )
        with self._lock:
            previous = self._entries.get(run_id)
            if previous is not None and previous.envelope_ref != entry.envelope_ref:
                self._remember_revoked_ref(previous.envelope_ref)
            self._entries[run_id] = entry
        scrub_host_envelope_payload(result)
        result.pop("host_envelope", None)
        result["auth_loaded"] = True
        result["session_token_ref"] = binding["session_token_ref"]
        result["managed_host_envelope_ref"] = entry.envelope_ref
        result["managed_host_envelope"] = {
            "schema_version": "mcp.managed_host_envelope.v1",
            "status": "staged",
            "managed_host_envelope_ref": entry.envelope_ref,
            "session_token_ref": binding["session_token_ref"],
            "runtime_context_id": binding["runtime_context_id"],
            "task_id": binding["task_id"],
            "raw_worker_auth_exposed": False,
            "raw_session_token_exposed": False,
            "raw_fence_token_exposed": False,
            "process_local": True,
            "startup_consumption_required": True,
        }
        return result

    def _capture_allocation(
        self,
        args: Mapping[str, Any],
        result: Any,
    ) -> Any:
        """Stage same-owner allocation auth before returning a public result."""

        if not isinstance(result, dict):
            return result
        # The transport adapter may retain aliased request/result objects for
        # audit; redact a detached public projection only.
        result = copy.deepcopy(result)
        if result.get("ok") is not True:
            _scrub_allocation_auth_payload(result)
            for field in _PUBLIC_AUTH_FLAG_FIELDS:
                result[field] = False
            return result

        session = result.get("same_owner_worker_session")
        context = result.get("context")
        session = session if isinstance(session, Mapping) else {}
        context = context if isinstance(context, Mapping) else {}
        scope = session.get("scope")
        scope = scope if isinstance(scope, Mapping) else {}
        evidence = result.get("branch_runtime_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}

        raw_session = _text(session.get("session_token"))
        response_fence = _text(context.get("fence_token"))
        request_fence = _text(args.get("fence_token"))
        raw_fence = response_fence or request_fence
        session_token_ref = _text(session.get("session_token_ref"))

        recovery_sources = (context, scope, evidence, args, result)

        def recovery_value(field: str, *aliases: str) -> str:
            for source in recovery_sources:
                for candidate in (field, *aliases):
                    value = _text(source.get(candidate))
                    if value:
                        return value
            return ""

        recovery_identity = {
            "project_id": recovery_value("project_id"),
            "backlog_id": recovery_value("backlog_id"),
            "runtime_context_id": recovery_value("runtime_context_id"),
            "task_id": recovery_value("task_id"),
            "parent_task_id": recovery_value("parent_task_id", "root_task_id"),
            "contract_execution_id": recovery_value(
                "contract_execution_id",
                "parent_task_id",
            ),
            "target_project_root": recovery_value(
                "target_project_root",
                "project_root",
                "repo_root",
                "worktree_path",
            ),
            "worker_id": recovery_value("worker_id"),
            "worker_slot_id": recovery_value("worker_slot_id", "worker_id"),
            "session_token_ref": session_token_ref,
        }

        # Same-owner issuance is optional for allocations owned by another
        # principal.  Such responses still receive the recursive public scrub.
        claims_same_owner_auth = bool(session)
        if not claims_same_owner_auth:
            scrub_host_envelope_payload(result)
            _scrub_allocation_auth_payload(result)
            result["auth_loaded"] = False
            for field in _PUBLIC_AUTH_FLAG_FIELDS:
                result[field] = False
            return result

        if response_fence and request_fence and not hmac.compare_digest(
            response_fence,
            request_fence,
        ):
            safe_error = _post_response_error(
                result,
                "managed_allocation_auth_mismatch",
                "Successful allocation fence did not match the exact request authority.",
                **{
                    key: value
                    for key, value in recovery_identity.items()
                    if value
                },
                durable_allocation_accepted=True,
                retry_requires_fresh_allocation=True,
            )
            _scrub_allocation_auth_payload(
                safe_error,
                raw_values=(raw_session, response_fence, request_fence),
            )
            for field in _PUBLIC_AUTH_FLAG_FIELDS:
                safe_error[field] = False
            return safe_error

        if not raw_session or not raw_fence or not session_token_ref:
            safe_error = _post_response_error(
                result,
                "managed_allocation_auth_missing",
                "Successful same-owner allocation did not deliver complete process-local auth.",
                **{
                    key: value
                    for key, value in recovery_identity.items()
                    if value
                },
                durable_allocation_accepted=True,
                retry_requires_fresh_allocation=True,
            )
            _scrub_allocation_auth_payload(
                safe_error,
                raw_values=(raw_session, raw_fence),
            )
            for field in _PUBLIC_AUTH_FLAG_FIELDS:
                safe_error[field] = False
            return safe_error

        route_sources = (args, result, context, evidence)
        route: dict[str, str] = {}
        for source in route_sources:
            route.update(_route_identity(source))

        sources = recovery_sources

        def first_value(field: str, *aliases: str) -> str:
            for source in sources:
                for candidate in (field, *aliases):
                    value = _text(source.get(candidate))
                    if value:
                        return value
            return ""

        binding: dict[str, str] = {
            "project_id": first_value("project_id"),
            "runtime_context_id": first_value("runtime_context_id"),
            "task_id": first_value("task_id"),
            "parent_task_id": first_value("parent_task_id", "root_task_id"),
            "contract_execution_id": first_value(
                "contract_execution_id",
                "parent_task_id",
            ),
            "target_project_root": first_value(
                "target_project_root",
                "project_root",
                "repo_root",
                "worktree_path",
            ),
            "session_token_ref": session_token_ref,
        }
        for field in _WORKER_BINDING_FIELDS:
            value = first_value(field)
            if value:
                binding[field] = value
        binding.update(route)

        staged_result = result
        staged_result.update(
            {
                key: value
                for key, value in binding.items()
                if value and key != "fence_token"
            }
        )
        staged_result["delivery"] = "worker_host_envelope"
        staged_result["host_envelope"] = {
            **binding,
            "route_identity": dict(route),
            "env": {
                "AMING_WORKER_SESSION_TOKEN": raw_session,
                "AMING_WORKER_FENCE_TOKEN": raw_fence,
            },
        }
        captured = self._capture_issuance(args, staged_result)
        if isinstance(captured, dict):
            _scrub_allocation_auth_payload(
                captured,
                raw_values=(raw_session, raw_fence),
            )
            for field in _PUBLIC_AUTH_FLAG_FIELDS:
                captured[field] = False
            if captured.get("auth_loaded") is True:
                captured["allocation_auth_staged"] = True
        return captured

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
        *,
        require_session_token_ref: bool = True,
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
            *_ROUTE_FIELDS,
        ]
        if require_session_token_ref:
            required.append("session_token_ref")
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

    @staticmethod
    def _normalize_exact_redundant_fence(
        tool_name: str,
        args: Mapping[str, Any],
        entry: _ManagedEnvelope,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Remove only a verifier-matched redundant managed fence.

        Older worker call shapes echo the allocation ``fence_token`` after a
        same-process host envelope has been staged.  The read-receipt Guide
        may instead carry its exact process-local environment reference.
        Neither is a second auth branch: the store must inject the
        authoritative credential.  Accept the canonical reference only for
        read receipt, or a verifier-matched redundant fence for other managed
        continuations.  Raw session auth, arbitrary environment references,
        and wrong fences remain local fail-closed rejections.
        """

        request_args = dict(args)
        supplied_fence = _text(request_args.get("fence_token"))
        if not supplied_fence or _text(request_args.get("session_token")):
            return request_args, None
        if (
            tool_name == "runtime_context_read_receipt"
            and supplied_fence == _MANAGED_FENCE_ENV_REF
        ):
            request_args.pop("fence_token", None)
            return request_args, None
        expected_hash = _text(entry.fence_token_hash)
        if not expected_hash or not hmac.compare_digest(
            _sha256(supplied_fence), expected_hash
        ):
            return request_args, _local_error(
                "managed_host_envelope_scope_mismatch",
                "Caller fence does not match the staged managed host envelope.",
                mismatched_fields=["fence_token"],
            )
        request_args.pop("fence_token", None)
        return request_args, None

    @staticmethod
    def _normalize_declared_host_auth_placeholders(
        args: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Consume only the exact public placeholders emitted by the Guide.

        Managed MCP callers can spread the canonical copy-safe body without
        receiving raw worker credentials. The process-local store remains the
        sole authority that realizes these two fields.
        """

        request_args = dict(args)
        placeholders = {
            "session_token": "<host-realized session_token>",
            "fence_token": "<host-realized fence_token>",
        }
        for field, placeholder in placeholders.items():
            supplied = _text(request_args.get(field))
            if supplied == placeholder:
                request_args.pop(field, None)
            elif supplied.startswith("<host-realized "):
                return request_args, _local_error(
                    "managed_host_envelope_auth_placeholder_invalid",
                    "Managed host auth placeholder does not match the canonical Guide.",
                    field=field,
                )
        return request_args, None

    def dispatch(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> Any:
        request_args = dict(args)
        if tool_name in ALLOCATION_TOOLS:
            missing = [
                field
                for field in ("project_id", "task_id")
                if not _text(request_args.get(field))
            ]
            if missing:
                return _local_error(
                    "managed_allocation_identity_incomplete",
                    "Managed allocation is missing immutable request identity.",
                    missing_fields=missing,
                )
            return self._capture_allocation(request_args, send(request_args))

        if tool_name in ISSUANCE_TOOLS:
            preflight = self._preflight_issuance(tool_name, request_args)
            if preflight is not None:
                return preflight
            if tool_name == "runtime_context_session_token_initial_join":
                supplied_managed_ref = _text(
                    request_args.get("managed_host_envelope_ref")
                )
                entry = self._entry_for(request_args)
                if entry is None and supplied_managed_ref:
                    with self._lock:
                        process_has_other_entries = bool(self._entries)
                    stale_or_replayed = (
                        process_has_other_entries
                        or self._is_revoked_ref(supplied_managed_ref)
                    )
                    return _local_error(
                        (
                            "managed_host_envelope_ref_stale_or_scope_mismatch"
                            if stale_or_replayed
                            else "managed_host_envelope_not_loaded"
                        ),
                        (
                            "Managed allocation ref is stale or belongs to another scope."
                            if stale_or_replayed
                            else "This MCP process has no exact staged allocation auth."
                        ),
                    )
                if entry is not None:
                    if not supplied_managed_ref:
                        return _local_error(
                            "managed_host_envelope_ref_required",
                            "Fresh initial join must bind the staged allocation opaque ref.",
                        )
                    if not hmac.compare_digest(
                        supplied_managed_ref,
                        entry.envelope_ref,
                    ):
                        return _local_error(
                            "managed_host_envelope_ref_stale_or_scope_mismatch",
                            "Managed allocation ref is stale or belongs to another scope.",
                        )
                    rejection = self._validate_continuation(
                        tool_name,
                        request_args,
                        entry,
                    )
                    if rejection is not None:
                        return rejection
                    request_args.pop("managed_host_envelope_ref", None)
                    with self._lock:
                        if entry.run_id in self._in_flight:
                            return _local_error(
                                "managed_host_envelope_concurrent_use",
                                "The exact process-local allocation auth already has an in-flight continuation.",
                            )
                        try:
                            store_state = self._store.synchronize(
                                entry.run_id,
                                lease_owner_id=_OWNER_ID,
                                envelope_ref=entry.envelope_ref,
                                expected_public_refs=entry.binding,
                            )
                        except HostEnvelopeError:
                            store_state = "unavailable"
                        if store_state != "active":
                            self._entries.pop(entry.run_id, None)
                            self._remember_revoked_ref(entry.envelope_ref)
                            return _local_error(
                                "managed_host_envelope_unavailable",
                                "The process-local allocation auth is absent or expired.",
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
                            self._remember_revoked_ref(entry.envelope_ref)
                            return _local_error(
                                "managed_host_envelope_unavailable",
                                "The process-local allocation auth is absent or expired.",
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
                            issuance_result = send(enriched)
                        finally:
                            enriched.pop("session_token", None)
                            enriched.pop("fence_token", None)
                            temporary_environment.clear()
                            delivery.discard()
                        captured = self._capture_issuance(
                            request_args,
                            issuance_result,
                        )
                    finally:
                        with self._lock:
                            self._in_flight.discard(entry.run_id)
                    if isinstance(captured, dict) and captured.get("auth_loaded") is True:
                        captured["allocation_auth_revoked"] = True
                        captured["previous_managed_host_envelope_revoked"] = True
                        captured["old_managed_host_envelope_ref_reusable"] = False
                    return captured
            request_args.pop("managed_host_envelope_ref", None)
            return self._capture_issuance(request_args, send(request_args))

        entry = self._entry_for(request_args)
        if entry is None:
            supplied_managed_ref = _text(
                request_args.get("managed_host_envelope_ref")
            )
            if supplied_managed_ref:
                with self._lock:
                    process_has_other_entries = bool(self._entries)
                stale_or_replayed = (
                    process_has_other_entries
                    or self._is_revoked_ref(supplied_managed_ref)
                )
                return _local_error(
                    (
                        "managed_host_envelope_ref_stale_or_scope_mismatch"
                        if stale_or_replayed
                        else "managed_host_envelope_not_loaded"
                    ),
                    (
                        "Managed allocation ref is stale or belongs to another scope."
                        if stale_or_replayed
                        else "This MCP process has no exact staged allocation auth."
                    ),
                )
            if tool_name == "contract_runtime_submit_line":
                # This generic facade also serves observer and QA writers.
                # Borrow worker auth only when an exact managed worker entry
                # exists; otherwise preserve the server's role gate.
                return send(request_args)
            candidate_binding = {
                field: _text(request_args.get(field))
                for field in ("project_id", "runtime_context_id", "task_id")
            }
            if all(candidate_binding.values()):
                with self._lock:
                    if _run_id(candidate_binding) in self._in_flight:
                        return _local_error(
                            "managed_host_envelope_concurrent_use",
                            "The exact process-local host envelope already has an in-flight continuation.",
                        )
            if (
                tool_name in {"graph_query", "runtime_context_worker_guide"}
                or not _text(request_args.get("session_token_ref"))
            ):
                return send(request_args)
            return _local_error(
                "managed_host_envelope_not_loaded",
                "This MCP process has no exact staged worker host envelope.",
            )
        supplied_managed_ref = _text(request_args.get("managed_host_envelope_ref"))
        if supplied_managed_ref and not hmac.compare_digest(
            supplied_managed_ref,
            entry.envelope_ref,
        ):
            return _local_error(
                "managed_host_envelope_ref_stale_or_scope_mismatch",
                "Managed allocation ref is stale or belongs to another scope.",
            )
        request_args.pop("managed_host_envelope_ref", None)
        request_args, placeholder_rejection = (
            self._normalize_declared_host_auth_placeholders(request_args)
        )
        if placeholder_rejection is not None:
            return placeholder_rejection
        request_args, redundant_fence_rejection = (
            self._normalize_exact_redundant_fence(tool_name, request_args, entry)
        )
        if redundant_fence_rejection is not None:
            return redundant_fence_rejection
        with self._lock:
            if entry.run_id in self._in_flight:
                return _local_error(
                    "managed_host_envelope_concurrent_use",
                    "The exact process-local host envelope already has an in-flight continuation.",
                )
            if entry.expired:
                store_state = "expired"
            else:
                try:
                    store_state = self._store.synchronize(
                        entry.run_id,
                        lease_owner_id=_OWNER_ID,
                        envelope_ref=entry.envelope_ref,
                        expected_public_refs=entry.binding,
                    )
                except HostEnvelopeError:
                    return _local_error(
                        "managed_host_envelope_unavailable",
                        "The process-local worker host envelope is unavailable.",
                    )
            if store_state == "unavailable":
                return _local_error(
                    "managed_host_envelope_unavailable",
                    "The process-local worker host envelope is unavailable.",
                )
            if store_state == "expired":
                if not entry.expired:
                    entry = _ManagedEnvelope(
                        run_id=entry.run_id,
                        envelope_ref=entry.envelope_ref,
                        binding=entry.binding,
                        fence_token_hash=entry.fence_token_hash,
                        expired=True,
                    )
                    self._entries[entry.run_id] = entry
                recovery_rejection = self._validate_continuation(
                    tool_name,
                    request_args,
                    entry,
                    require_session_token_ref=False,
                )
                if recovery_rejection is not None:
                    return recovery_rejection
                if (
                    tool_name != "runtime_context_worker_guide"
                    or _text(request_args.get("session_token_ref"))
                ):
                    return _local_error(
                        "managed_host_envelope_unavailable",
                        "The process-local worker host envelope is absent or expired.",
                    )
                self._entries.pop(entry.run_id, None)
                self._in_flight.add(entry.run_id)
                expired_recovery = True
            else:
                rejection = self._validate_continuation(
                    tool_name,
                    request_args,
                    entry,
                )
                if rejection is not None:
                    return rejection
                self._in_flight.add(entry.run_id)
                expired_recovery = False

        if expired_recovery:
            try:
                return send(request_args)
            finally:
                with self._lock:
                    self._in_flight.discard(entry.run_id)

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
            tool_name in {"parallel_branch_startup", "runtime_context_finish_gate"}
            and isinstance(result, dict)
            and result.get("ok") is True
            and _text(result.get("status"))
            in {
                "accepted",
                "started",
                "startup_recorded",
                "running",
                "passed",
                "finish_gate_recorded",
            }
        ):
            self._store.acknowledge(
                entry.run_id,
                lease_owner_id=_OWNER_ID,
                envelope_ref=entry.envelope_ref,
                expected_public_refs=entry.binding,
            )
            self._remember_revoked_ref(entry.envelope_ref)
            with self._lock:
                self._entries.pop(entry.run_id, None)
            result["managed_host_envelope_consumed"] = True
            result["managed_host_envelope_consumed_at"] = tool_name
            result["raw_worker_auth_exposed"] = False
        return result


_DEFAULT_CONTINUITY = ManagedHostEnvelopeContinuity()


def default_managed_host_envelope_continuity() -> ManagedHostEnvelopeContinuity:
    return _DEFAULT_CONTINUITY
