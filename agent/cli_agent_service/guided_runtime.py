"""Thin observer-runtime bridge into governed CLI Agent Service admission."""

from __future__ import annotations

from typing import Any, Mapping

from .launchers import WORKER_AUTH_ENV_KEYS, scrub_host_envelope_payload
from .service import (
    DEFAULT_SOCKET_TIMEOUT_SECONDS,
    ServiceError,
    ServicePaths,
    ServiceUnavailableError,
    request_service,
)


GUIDED_RUNTIME_DISPATCH_SCHEMA_VERSION = "cli_agent_service.guided_runtime_dispatch.v1"

_ADMISSION_FIELDS = frozenset(
    {
        "authority_selectors",
    }
)
_AUTHORITY_FIELDS = frozenset(
    {
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "worker_id",
        "worker_slot_id",
        "observer_command_id",
        "role",
        "profile_id",
        "principal_id",
        "expected_execution_state_revision",
        "expected_execution_state_hash",
        "expected_dispatch_identity_hash",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
        "harness",
        "provider",
        "model",
        "runtime_id",
        "endpoint_id",
        "launcher_id",
        "backend_mode",
    }
)
_REQUIRED_AUTHORITY_FIELDS = (
    "project_id",
    "backlog_id",
    "contract_execution_id",
    "runtime_context_id",
    "task_id",
    "worker_id",
    "worker_slot_id",
    "observer_command_id",
    "role",
    "profile_id",
    "principal_id",
    "expected_execution_state_hash",
    "expected_dispatch_identity_hash",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
    "backend_mode",
)


class GuidedRuntimeDispatchError(RuntimeError):
    """A governed service dispatch failed without invoking a local fallback."""

    def __init__(self, message: str, *, status: str = "blocked") -> None:
        super().__init__(message)
        self.status = status


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GuidedRuntimeDispatchError(
            "guided runtime admission requires {}".format(field_name)
        )
    return dict(value)


def _authority_selectors(
    value: Any,
    *,
    project_id: str,
    backlog_id: str,
) -> dict[str, Any]:
    selectors = _mapping(value, "ContractRuntime authority selectors")
    unsupported = sorted(set(selectors) - _AUTHORITY_FIELDS)
    if unsupported:
        raise GuidedRuntimeDispatchError(
            "guided runtime selectors contain unsupported authority fields"
        )
    missing = [
        field_name
        for field_name in _REQUIRED_AUTHORITY_FIELDS
        if not _text(selectors.get(field_name))
    ]
    if missing:
        raise GuidedRuntimeDispatchError(
            "guided runtime selectors are incomplete: {}".format(
                ", ".join(missing)
            )
        )
    try:
        revision = int(selectors.get("expected_execution_state_revision") or 0)
    except (TypeError, ValueError) as exc:
        raise GuidedRuntimeDispatchError(
            "guided runtime selector revision is invalid"
        ) from exc
    if revision <= 0:
        raise GuidedRuntimeDispatchError(
            "guided runtime selectors require current authority coordinates"
        )
    if _text(selectors.get("project_id")) != _text(project_id):
        raise GuidedRuntimeDispatchError(
            "guided runtime selector project does not match the observer request"
        )
    if _text(selectors.get("backlog_id")) != _text(backlog_id):
        raise GuidedRuntimeDispatchError(
            "guided runtime selector backlog does not match the observer request"
        )
    if _text(selectors.get("principal_id")) != _text(selectors.get("worker_id")):
        raise GuidedRuntimeDispatchError(
            "guided runtime principal must match canonical dispatch identity"
        )
    selectors["expected_execution_state_revision"] = revision
    return selectors


def _transient_host_envelope(value: Any) -> dict[str, Any]:
    envelope = _mapping(value, "one transient worker host envelope")
    if set(envelope) != {"env"}:
        raise GuidedRuntimeDispatchError(
            "guided runtime accepts only a transient worker auth envelope"
        )
    environment = _mapping(envelope.get("env"), "worker host envelope auth")
    if set(environment) != set(WORKER_AUTH_ENV_KEYS):
        raise GuidedRuntimeDispatchError(
            "guided runtime worker host envelope has invalid auth fields"
        )
    normalized = {key: environment.get(key) for key in WORKER_AUTH_ENV_KEYS}
    if any(
        not isinstance(value, str) or not value or "\x00" in value
        for value in normalized.values()
    ):
        raise GuidedRuntimeDispatchError(
            "guided runtime worker host envelope is incomplete"
        )
    return {"env": normalized}


def request_guided_runtime(
    *,
    admission: Mapping[str, Any],
    project_id: str,
    backlog_id: str,
    transient_host_envelope: Mapping[str, Any],
    state_dir: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Submit canonical ticket selectors for daemon-owned run admission."""

    admission_value = _mapping(admission, "guided service admission")
    if set(admission_value) - _ADMISSION_FIELDS:
        raise GuidedRuntimeDispatchError(
            "guided service admission contains unsupported fields"
        )
    selectors = _authority_selectors(
        admission_value.get("authority_selectors"),
        project_id=project_id,
        backlog_id=backlog_id,
    )
    envelope: dict[str, Any] = {}
    try:
        envelope = _transient_host_envelope(transient_host_envelope)
        try:
            response = request_service(
                ServicePaths.from_state_dir(state_dir or None),
                "start_host_envelope_run",
                payload={
                    "authority_selectors": selectors,
                    "host_envelope": envelope,
                },
                timeout_seconds=timeout_seconds,
            )
        except ServiceUnavailableError as exc:
            raise GuidedRuntimeDispatchError(
                "CLI Agent Service is unavailable",
                status="unavailable",
            ) from exc
        except ServiceError as exc:
            raise GuidedRuntimeDispatchError(
                "CLI Agent Service rejected the governed run: {}".format(exc),
                status="rejected",
            ) from exc
    finally:
        scrub_host_envelope_payload(envelope)
        scrub_host_envelope_payload(transient_host_envelope)
    if response.get("ok") is not True or response.get("status") != "started":
        raise GuidedRuntimeDispatchError(
            _text(response.get("error"))
            or "CLI Agent Service rejected the governed run",
            status=_text(response.get("status")) or "rejected",
        )
    response_identity = {
        "role": response.get("role"),
        "profile_id": response.get("profile_id"),
        "principal_id": response.get("principal_id"),
        "runtime_context_id": response.get("runtime_context_id"),
        "task_id": response.get("task_id"),
        "contract_execution_id": response.get("contract_execution_id"),
    }
    mismatches = [
        field_name
        for field_name, actual in response_identity.items()
        if _text(actual) != _text(selectors.get(field_name))
    ]
    if not _text(response.get("run_id")):
        mismatches.append("run_id")
    for field_name in (
        "direct_invocation_fallback",
        "caller_run_accepted",
        "caller_prompt_accepted",
        "caller_environment_accepted",
    ):
        if response.get(field_name) is not False:
            mismatches.append(field_name)
    for field_name in (
        "transient_host_envelope_required",
        "transient_host_envelope_accepted",
        "transient_host_envelope_consumed",
        "provider_output_suppressed",
    ):
        if response.get(field_name) is not True:
            mismatches.append(field_name)
    for field_name in (
        "transient_host_envelope_persisted",
        "host_envelope_run_authority",
        "raw_session_token_persisted",
        "raw_fence_token_persisted",
        "raw_provider_output_persisted",
    ):
        if response.get(field_name) is not False:
            mismatches.append(field_name)
    if mismatches:
        raise GuidedRuntimeDispatchError(
            "CLI Agent Service returned mismatched governed run identity: {}".format(
                ", ".join(sorted(set(mismatches)))
            ),
            status="rejected",
        )
    canonical_run_id = _text(response.get("run_id"))
    official_runtime_startup_identity = {
        "actual_host_worker_id": canonical_run_id,
        "worker_session_id": canonical_run_id,
        "filer_principal": canonical_run_id,
        "worker_transcript_ref": "codex:{}".format(canonical_run_id),
    }
    return {
        "schema_version": GUIDED_RUNTIME_DISPATCH_SCHEMA_VERSION,
        "ok": True,
        "status": "started",
        "operation": "start_host_envelope_run",
        "run_id": canonical_run_id,
        "official_runtime_startup_identity": official_runtime_startup_identity,
        "role": _text(response.get("role")),
        "profile_id": _text(response.get("profile_id")),
        "principal_id": _text(response.get("principal_id")),
        "runtime_context_id": _text(response.get("runtime_context_id")),
        "task_id": _text(response.get("task_id")),
        "contract_execution_id": _text(response.get("contract_execution_id")),
        "authority_selectors": dict(selectors),
        "service_response": dict(response),
        "direct_invocation_fallback": False,
        "raw_session_token_exposed": False,
        "raw_fence_token_exposed": False,
        "caller_run_accepted": False,
        "caller_prompt_accepted": False,
        "caller_environment_accepted": False,
        "transient_host_envelope_required": True,
        "transient_host_envelope_accepted": True,
        "transient_host_envelope_consumed": True,
        "transient_host_envelope_persisted": False,
        "host_envelope_run_authority": False,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "raw_provider_output_persisted": False,
        "provider_output_suppressed": True,
        "governance_authority": False,
        "operational_dispatch_only": True,
    }


def request_contract_runtime_observer(
    *,
    current_state: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
    profile_requirements: Mapping[str, Any],
    transient_host_envelope: Mapping[str, Any],
    state_dir: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Resolve and dispatch the supported source-backed L2 observer path."""
    from agent.governance.contract_state_runtime import (
        resolve_cli_agent_observer_admission,
    )

    admission = resolve_cli_agent_observer_admission(
        current_state,
        runtime_identity=runtime_identity,
        profile_requirements=profile_requirements,
    )
    selectors = admission["authority_selectors"]
    return request_guided_runtime(
        admission={"authority_selectors": selectors},
        project_id=selectors["project_id"],
        backlog_id=selectors["backlog_id"],
        transient_host_envelope=transient_host_envelope,
        state_dir=state_dir,
        timeout_seconds=timeout_seconds,
    )
