"""Thin observer-runtime bridge into governed CLI Agent Service admission."""

from __future__ import annotations

from typing import Any, Mapping

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


def request_guided_runtime(
    *,
    admission: Mapping[str, Any],
    project_id: str,
    backlog_id: str,
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
    try:
        response = request_service(
            ServicePaths.from_state_dir(state_dir or None),
            "start_host_envelope_run",
            payload={"authority_selectors": selectors},
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
        "caller_host_envelope_accepted",
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
    return {
        "schema_version": GUIDED_RUNTIME_DISPATCH_SCHEMA_VERSION,
        "ok": True,
        "status": "started",
        "operation": "start_host_envelope_run",
        "run_id": _text(response.get("run_id")),
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
        "caller_host_envelope_accepted": False,
        "governance_authority": False,
        "operational_dispatch_only": True,
    }
