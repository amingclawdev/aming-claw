"""Thin observer-runtime bridge into governed CLI Agent Service admission."""

from __future__ import annotations

from typing import Any, Mapping

from .launchers import WORKER_AUTH_ENV_KEYS
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
        "run",
        "authority_selectors",
        "host_envelope",
        "ttl_seconds",
        "expires_at",
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
)
_HOST_REFERENCE_FIELDS = (
    "project_id",
    "backlog_id",
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "worker_role",
    "worker_id",
    "worker_slot_id",
    "actual_host_worker_id",
    "worker_session_id",
    "session_token_ref",
)
_CANONICAL_HOST_REFERENCES = {
    "project_id": "project_id",
    "backlog_id": "backlog_id",
    "runtime_context_id": "runtime_context_id",
    "task_id": "task_id",
    "worker_role": "role",
    "worker_id": "worker_id",
    "worker_slot_id": "worker_slot_id",
}


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


def _validate_public_run(
    run: Mapping[str, Any],
    selectors: Mapping[str, Any],
) -> str:
    run_id = _text(run.get("run_id"))
    profile = _mapping(run.get("profile"), "a public immutable profile")
    config = _mapping(run.get("config"), "a public resolved run config")
    if not run_id:
        raise GuidedRuntimeDispatchError("guided runtime public run_id is required")
    comparisons = {
        "project_id": config.get("project_id"),
        "role": config.get("role"),
        "profile_id": config.get("profile_id"),
    }
    mismatches = [
        field_name
        for field_name, actual in comparisons.items()
        if _text(actual) != _text(selectors.get(field_name))
    ]
    if _text(profile.get("profile_id")) != _text(selectors.get("profile_id")):
        mismatches.append("profile.profile_id")
    if mismatches:
        raise GuidedRuntimeDispatchError(
            "guided runtime public run conflicts with canonical selectors: {}".format(
                ", ".join(sorted(set(mismatches)))
            )
        )
    return run_id


def _host_envelope(
    value: Any,
    *,
    selectors: Mapping[str, Any],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    supplied = _mapping(value, "copy-safe host envelope references")
    if "env" in supplied or any(key in supplied for key in WORKER_AUTH_ENV_KEYS):
        raise GuidedRuntimeDispatchError(
            "guided runtime host references must not contain raw auth"
        )
    envelope: dict[str, Any] = {}
    for envelope_field, selector_field in _CANONICAL_HOST_REFERENCES.items():
        canonical = _text(selectors.get(selector_field))
        supplied_value = _text(supplied.get(envelope_field))
        if supplied_value and supplied_value != canonical:
            raise GuidedRuntimeDispatchError(
                "guided runtime host reference conflicts with canonical selectors"
            )
        if canonical:
            envelope[envelope_field] = canonical
    for field_name in _HOST_REFERENCE_FIELDS:
        if field_name in envelope:
            continue
        value_text = _text(supplied.get(field_name))
        if value_text:
            envelope[field_name] = value_text
    auth_environment = {
        key: _text(environment.get(key)) for key in WORKER_AUTH_ENV_KEYS
    }
    missing_auth = [key for key, raw_value in auth_environment.items() if not raw_value]
    if missing_auth:
        raise GuidedRuntimeDispatchError(
            "guided runtime host envelope is missing worker auth"
        )
    envelope["env"] = auth_environment
    return envelope


def request_guided_runtime(
    *,
    admission: Mapping[str, Any],
    project_id: str,
    backlog_id: str,
    prompt: str,
    worktree: str,
    environment: Mapping[str, str],
    state_dir: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Submit one canonical, host-enveloped run to CLI Agent Service."""

    admission_value = _mapping(admission, "guided service admission")
    if set(admission_value) - _ADMISSION_FIELDS:
        raise GuidedRuntimeDispatchError(
            "guided service admission contains unsupported fields"
        )
    public_run = _mapping(admission_value.get("run"), "a public run request")
    selectors = _authority_selectors(
        admission_value.get("authority_selectors"),
        project_id=project_id,
        backlog_id=backlog_id,
    )
    run_id = _validate_public_run(public_run, selectors)
    canonical_worktree = _text(worktree)
    if not canonical_worktree:
        raise GuidedRuntimeDispatchError(
            "guided runtime worker worktree is required"
        )
    envelope = _host_envelope(
        admission_value.get("host_envelope") or {},
        selectors=selectors,
        environment=environment,
    )
    payload: dict[str, Any] = {
        "run": public_run,
        "worktree": canonical_worktree,
        "prompt": str(prompt),
        "authority_selectors": selectors,
        "host_envelope": envelope,
    }
    for field_name in ("ttl_seconds", "expires_at"):
        if admission_value.get(field_name) not in (None, ""):
            payload[field_name] = admission_value[field_name]
    try:
        response = request_service(
            ServicePaths.from_state_dir(state_dir or None),
            "start_host_envelope_run",
            payload=payload,
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
    return {
        "schema_version": GUIDED_RUNTIME_DISPATCH_SCHEMA_VERSION,
        "ok": True,
        "status": "started",
        "operation": "start_host_envelope_run",
        "run_id": _text(response.get("run_id")) or run_id,
        "role": _text(selectors.get("role")),
        "profile_id": _text(selectors.get("profile_id")),
        "principal_id": _text(selectors.get("principal_id")),
        "runtime_context_id": _text(selectors.get("runtime_context_id")),
        "task_id": _text(selectors.get("task_id")),
        "contract_execution_id": _text(selectors.get("contract_execution_id")),
        "authority_selectors": dict(selectors),
        "service_response": dict(response),
        "direct_invocation_fallback": False,
        "raw_session_token_exposed": False,
        "raw_fence_token_exposed": False,
        "governance_authority": False,
        "operational_dispatch_only": True,
    }
