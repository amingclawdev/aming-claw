"""Explainable configuration resolution for immutable CLI Agent Service runs."""

from __future__ import annotations

from typing import Any, Mapping

from .models import (
    PUBLIC_CONFIGURATION_FIELDS,
    AgentProfile,
    AgentRun,
    FieldResolution,
    ResolutionCandidate,
    ResolvedAgentConfig,
)


PROFILE_PRECEDENCE = 100
RUN_REQUEST_PRECEDENCE = 90
PROJECT_ROLE_PRECEDENCE = 70
PIPELINE_ROLE_PRECEDENCE = 60
PIPELINE_DEFAULT_PRECEDENCE = 50
COMPATIBILITY_DEFAULT_PRECEDENCE = 10
INTRINSIC_DEFAULT_PRECEDENCE = 1

_ROUTING_FIELDS = (
    "provider",
    "model",
    "backend_mode",
    "auth_mode",
    "output_policy",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _role_entry(config: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    return _mapping(_mapping(config.get("roles")).get(role.lower()))


def _project_role_entry(project_config: Any, role: str) -> Mapping[str, Any]:
    if isinstance(project_config, Mapping):
        ai_config = _mapping(project_config.get("ai"))
        routing = _mapping(ai_config.get("routing"))
        if not routing:
            routing = _mapping(project_config.get("routing"))
    else:
        ai_config = getattr(project_config, "ai", None)
        routing = _mapping(getattr(ai_config, "routing", None))
    return _mapping(routing.get(role.lower()))


def _default_backend_mode(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized == "openai":
        return "codex_cli"
    if normalized == "anthropic":
        return "claude_cli"
    if normalized == "fixture":
        return "fixture"
    return ""


def _default_auth_mode(backend_mode: str) -> str:
    normalized = str(backend_mode or "").strip().lower()
    if normalized.endswith("_api"):
        return "api_key_env"
    if normalized.endswith("_cli"):
        return "cli_auth"
    if normalized == "docker_live_ai":
        return "external_harness"
    if normalized == "fixture":
        return "not_required"
    return ""


def _selected_resolution(
    field_name: str,
    candidates: list[ResolutionCandidate],
) -> FieldResolution:
    ordered = sorted(candidates, key=lambda candidate: candidate.precedence, reverse=True)
    selected_index = next(
        (index for index, candidate in enumerate(ordered) if candidate.value),
        0,
    )
    selected = ordered[selected_index]
    annotated = tuple(
        ResolutionCandidate(
            value=candidate.value,
            source=candidate.source,
            precedence=candidate.precedence,
            selected=index == selected_index,
        )
        for index, candidate in enumerate(ordered)
    )
    return FieldResolution(
        field_name=field_name,
        value=selected.value,
        source=selected.source,
        precedence=selected.precedence,
        candidates=annotated,
    )


def _validate_routing(resolved: Mapping[str, str]) -> None:
    if not resolved.get("backend_mode"):
        return
    try:
        from pipeline_config import validate_invocation_routing
    except ImportError:
        from agent.pipeline_config import validate_invocation_routing

    errors = validate_invocation_routing(
        provider=resolved.get("provider", ""),
        model=resolved.get("model", ""),
        backend_mode=resolved.get("backend_mode", ""),
        auth_mode=resolved.get("auth_mode", ""),
    )
    if errors:
        raise ValueError("Invalid resolved CLI agent routing: {}".format("; ".join(errors)))


def resolve_agent_config(
    *,
    run_id: str,
    role: str,
    project_id: str,
    profile: AgentProfile | None = None,
    existing_run: AgentRun | None = None,
    pipeline_config: Mapping[str, Any] | None = None,
    project_config: Any = None,
    compatibility_defaults: Mapping[str, Any] | None = None,
    governance_refs: Mapping[str, str] | None = None,
    created_at: str = "",
    parent_run_id: str = "",
    successor_of_run_id: str = "",
) -> AgentRun:
    """Resolve and pin one run without loading credential material.

    Precedence is immutable profile, project role, pipeline role, pipeline
    default, then explicit compatibility defaults. An existing run is returned
    unchanged, so later legacy reads cannot rewrite its selected profile.
    """
    run_id = str(run_id or "").strip()
    role = str(role or "").strip().lower()
    project_id = str(project_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    if not role:
        raise ValueError("role is required")
    if not project_id:
        raise ValueError("project_id is required")
    if existing_run is not None:
        if existing_run.run_id != run_id:
            raise ValueError("existing run_id does not match requested run_id")
        return existing_run

    if profile is not None:
        if profile.role_policy.roles and role not in profile.role_policy.roles:
            raise ValueError("role '{}' is not allowed by selected profile".format(role))
        if (
            profile.role_policy.project_ids
            and project_id not in profile.role_policy.project_ids
        ):
            raise ValueError(
                "project '{}' is not allowed by selected profile".format(project_id)
            )
        credential_provider = profile.credential_ref.provider
        if (
            credential_provider
            and credential_provider != profile.inference_endpoint.provider
        ):
            raise ValueError("credential reference provider does not match endpoint")

    candidates: dict[str, list[ResolutionCandidate]] = {
        field_name: [
            ResolutionCandidate(
                value="",
                source="unresolved",
                precedence=0,
            )
        ]
        for field_name in PUBLIC_CONFIGURATION_FIELDS
    }

    def add(field_name: str, value: Any, source: str, precedence: int) -> None:
        if field_name not in candidates:
            return
        candidates[field_name].append(
            ResolutionCandidate(
                value=str(value or "").strip(),
                source=source,
                precedence=precedence,
            )
        )

    add("project_id", project_id, "run_request.project_id", RUN_REQUEST_PRECEDENCE)
    add("role", role, "run_request.role", RUN_REQUEST_PRECEDENCE)
    add(
        "output_policy",
        "hash_and_summary_only",
        "intrinsic_default",
        INTRINSIC_DEFAULT_PRECEDENCE,
    )

    if profile is not None:
        endpoint = profile.inference_endpoint
        for field_name, value in {
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "runtime_id": profile.harness_runtime.runtime_id,
            "runtime_version": profile.harness_runtime.version,
            "endpoint_id": endpoint.endpoint_id,
            "endpoint_version": endpoint.version,
            "credential_ref": profile.credential_ref.ref_id,
            "credential_ref_version": profile.credential_ref.version,
            "launcher_id": profile.launcher_adapter.launcher_id,
            "launcher_version": profile.launcher_adapter.version,
            "role_policy_id": profile.role_policy.policy_id,
            "role_policy_version": profile.role_policy.version,
            "provider": endpoint.provider,
            "model": endpoint.model,
            "backend_mode": endpoint.backend_mode,
            "auth_mode": endpoint.auth_mode,
            "output_policy": profile.output_policy,
        }.items():
            add(field_name, value, "agent_profile", PROFILE_PRECEDENCE)

    project_role = _project_role_entry(project_config, role)
    for field_name in _ROUTING_FIELDS:
        add(
            field_name,
            project_role.get(field_name),
            "project_config.ai.routing.{}".format(role),
            PROJECT_ROLE_PRECEDENCE,
        )

    pipeline = _mapping(pipeline_config)
    pipeline_role = _role_entry(pipeline, role)
    for field_name in _ROUTING_FIELDS:
        add(
            field_name,
            pipeline_role.get(field_name),
            "pipeline_config.roles.{}".format(role),
            PIPELINE_ROLE_PRECEDENCE,
        )
    pipeline_default = _mapping(pipeline.get("default"))
    for field_name in _ROUTING_FIELDS:
        add(
            field_name,
            pipeline_default.get(field_name),
            "pipeline_config.default",
            PIPELINE_DEFAULT_PRECEDENCE,
        )

    defaults = _mapping(compatibility_defaults)
    for field_name in PUBLIC_CONFIGURATION_FIELDS:
        add(
            field_name,
            defaults.get(field_name),
            "compatibility_defaults",
            COMPATIBILITY_DEFAULT_PRECEDENCE,
        )

    provider_resolution = _selected_resolution("provider", candidates["provider"])
    derived_backend = _default_backend_mode(provider_resolution.value)
    if derived_backend:
        add(
            "backend_mode",
            derived_backend,
            "{}:derived_backend_mode".format(provider_resolution.source),
            max(provider_resolution.precedence - 1, INTRINSIC_DEFAULT_PRECEDENCE),
        )
    backend_resolution = _selected_resolution(
        "backend_mode",
        candidates["backend_mode"],
    )
    derived_auth = _default_auth_mode(backend_resolution.value)
    if derived_auth:
        add(
            "auth_mode",
            derived_auth,
            "{}:derived_auth_mode".format(backend_resolution.source),
            max(backend_resolution.precedence - 1, INTRINSIC_DEFAULT_PRECEDENCE),
        )

    resolutions = tuple(
        _selected_resolution(field_name, candidates[field_name])
        for field_name in PUBLIC_CONFIGURATION_FIELDS
    )
    values = {resolution.field_name: resolution.value for resolution in resolutions}
    _validate_routing(values)

    return AgentRun(
        run_id=run_id,
        profile=profile,
        config=ResolvedAgentConfig(
            resolutions=resolutions,
            **values,
        ),
        governance_refs=tuple((governance_refs or {}).items()),
        created_at=str(created_at or "").strip(),
        parent_run_id=str(parent_run_id or "").strip(),
        successor_of_run_id=str(successor_of_run_id or "").strip(),
    )


# One resolver implementation; this name reads naturally at launch call sites.
resolve_agent_run = resolve_agent_config
