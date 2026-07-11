"""Immutable, public-safe configuration models for the CLI Agent Service."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


HARNESS_RUNTIME_SCHEMA_VERSION = "cli_agent_service.harness_runtime.v1"
INFERENCE_ENDPOINT_SCHEMA_VERSION = "cli_agent_service.inference_endpoint.v1"
CREDENTIAL_REF_SCHEMA_VERSION = "cli_agent_service.credential_ref.v1"
LAUNCHER_ADAPTER_SCHEMA_VERSION = "cli_agent_service.launcher_adapter.v1"
ROLE_POLICY_SCHEMA_VERSION = "cli_agent_service.role_policy.v1"
AGENT_PROFILE_SCHEMA_VERSION = "cli_agent_service.agent_profile.v1"
RESOLVED_CONFIG_SCHEMA_VERSION = "cli_agent_service.resolved_config.v1"
AGENT_RUN_SCHEMA_VERSION = "cli_agent_service.agent_run.v1"
GOVERNANCE_REF_SCHEMA_VERSION = "cli_agent_service.governance_ref.v1"

PUBLIC_CONFIGURATION_FIELDS = (
    "profile_id",
    "profile_version",
    "runtime_id",
    "runtime_version",
    "endpoint_id",
    "endpoint_version",
    "credential_ref",
    "credential_ref_version",
    "launcher_id",
    "launcher_version",
    "role_policy_id",
    "role_policy_version",
    "provider",
    "model",
    "backend_mode",
    "auth_mode",
    "output_policy",
    "project_id",
    "role",
)


def _required(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("{} is required".format(field_name))
    return normalized


def _strings(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


_LOWER_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,191}")
_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_GOVERNANCE_REF_PATTERNS = {
    "project_id": _LOWER_ID_PATTERN,
    "backlog_id": re.compile(r"[A-Z][A-Z0-9-]{2,191}"),
    "task_id": _LOWER_ID_PATTERN,
    "parent_task_id": _LOWER_ID_PATTERN,
    "runtime_context_id": re.compile(r"mfrctx-[a-z0-9][a-z0-9-]{2,127}"),
    "contract_execution_id": re.compile(r"cex-[a-z0-9][a-z0-9-]{2,191}"),
    "route_id": re.compile(r"route-[a-z0-9][a-z0-9-]{2,191}"),
    "route_context_hash": _HASH_PATTERN,
    "prompt_contract_id": re.compile(r"rprompt-[a-z0-9][a-z0-9-]{2,191}"),
    "prompt_contract_hash": _HASH_PATTERN,
    "route_token_ref": re.compile(r"rtok-[0-9a-f]{16,128}"),
    "session_token_ref": re.compile(r"wstok-[0-9a-f]{16,128}"),
    "visible_injection_manifest_hash": _HASH_PATTERN,
    "graph_trace_id": re.compile(r"gqt-[a-z0-9][a-z0-9-]{2,191}"),
    "timeline_ref": re.compile(r"timeline:[0-9]+"),
    "commit_sha": re.compile(r"[0-9a-f]{40}"),
}


@dataclass(frozen=True)
class GovernanceRef:
    """One explicitly public-safe governance identifier or content hash."""

    name: str
    value: str
    schema_version: str = field(default=GOVERNANCE_REF_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        value = str(self.value or "").strip()
        pattern = _GOVERNANCE_REF_PATTERNS.get(name)
        if pattern is None:
            raise ValueError("governance reference '{}' is not public-safe".format(name))
        if not pattern.fullmatch(value):
            raise ValueError(
                "governance reference '{}' has an invalid public-safe value".format(name)
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "value", value)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "value": self.value,
            "public_safe": True,
        }


def _governance_refs(
    values: Mapping[str, str]
    | Iterable[GovernanceRef | tuple[str, str]],
) -> tuple[GovernanceRef, ...]:
    items = values.items() if isinstance(values, Mapping) else values
    refs = tuple(
        item if isinstance(item, GovernanceRef) else GovernanceRef(*item)
        for item in items
    )
    names = tuple(ref.name for ref in refs)
    if len(names) != len(set(names)):
        raise ValueError("governance references must be unique")
    return tuple(sorted(refs, key=lambda ref: ref.name))


@dataclass(frozen=True)
class HarnessRuntime:
    runtime_id: str
    version: str = "1"
    kind: str = ""
    executable_ref: str = ""
    capabilities: tuple[str, ...] = ()
    schema_version: str = field(
        default=HARNESS_RUNTIME_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_id", _required(self.runtime_id, "runtime_id"))
        object.__setattr__(self, "version", _required(self.version, "version"))
        object.__setattr__(self, "kind", str(self.kind or "").strip())
        object.__setattr__(
            self,
            "executable_ref",
            str(self.executable_ref or "").strip(),
        )
        object.__setattr__(self, "capabilities", _strings(self.capabilities))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "version": self.version,
            "kind": self.kind,
            "executable_ref": self.executable_ref,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class InferenceEndpoint:
    endpoint_id: str
    provider: str
    model: str
    backend_mode: str
    auth_mode: str
    version: str = "1"
    endpoint_kind: str = ""
    schema_version: str = field(
        default=INFERENCE_ENDPOINT_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "endpoint_id",
            "provider",
            "model",
            "backend_mode",
            "auth_mode",
            "version",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(
            self,
            "endpoint_kind",
            str(self.endpoint_kind or "").strip(),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "endpoint_id": self.endpoint_id,
            "version": self.version,
            "provider": self.provider,
            "model": self.model,
            "backend_mode": self.backend_mode,
            "auth_mode": self.auth_mode,
            "endpoint_kind": self.endpoint_kind,
        }


@dataclass(frozen=True)
class CredentialRef:
    ref_id: str
    version: str = "1"
    provider: str = ""
    ref_kind: str = "host_owned"
    schema_version: str = field(
        default=CREDENTIAL_REF_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref_id", _required(self.ref_id, "ref_id"))
        object.__setattr__(self, "version", _required(self.version, "version"))
        object.__setattr__(self, "provider", str(self.provider or "").strip())
        object.__setattr__(self, "ref_kind", _required(self.ref_kind, "ref_kind"))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "credential_ref": self.ref_id,
            "version": self.version,
            "provider": self.provider,
            "ref_kind": self.ref_kind,
            "raw_credential_material_exposed": False,
        }


@dataclass(frozen=True)
class LauncherAdapter:
    launcher_id: str
    version: str = "1"
    kind: str = "process"
    environment_keys: tuple[str, ...] = ()
    supports_host_handoff: bool = False
    schema_version: str = field(
        default=LAUNCHER_ADAPTER_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "launcher_id", _required(self.launcher_id, "launcher_id"))
        object.__setattr__(self, "version", _required(self.version, "version"))
        object.__setattr__(self, "kind", _required(self.kind, "kind"))
        object.__setattr__(
            self,
            "environment_keys",
            _strings(self.environment_keys),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "launcher_id": self.launcher_id,
            "version": self.version,
            "kind": self.kind,
            "environment_keys": list(self.environment_keys),
            "supports_host_handoff": self.supports_host_handoff,
            "raw_environment_exposed": False,
        }


@dataclass(frozen=True)
class RolePolicy:
    policy_id: str
    version: str = "1"
    roles: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    max_concurrency: int = 1
    timeout_sec: int = 120
    successor_budget: int = 0
    schema_version: str = field(default=ROLE_POLICY_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _required(self.policy_id, "policy_id"))
        object.__setattr__(self, "version", _required(self.version, "version"))
        object.__setattr__(self, "roles", _strings(self.roles))
        object.__setattr__(self, "project_ids", _strings(self.project_ids))
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.timeout_sec < 1:
            raise ValueError("timeout_sec must be at least 1")
        if self.successor_budget < 0:
            raise ValueError("successor_budget cannot be negative")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "roles": list(self.roles),
            "project_ids": list(self.project_ids),
            "max_concurrency": self.max_concurrency,
            "timeout_sec": self.timeout_sec,
            "successor_budget": self.successor_budget,
        }


@dataclass(frozen=True)
class AgentProfile:
    profile_id: str
    harness_runtime: HarnessRuntime
    inference_endpoint: InferenceEndpoint
    credential_ref: CredentialRef
    launcher_adapter: LauncherAdapter
    role_policy: RolePolicy
    version: str = "1"
    output_policy: str = "hash_and_summary_only"
    schema_version: str = field(default=AGENT_PROFILE_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _required(self.profile_id, "profile_id"))
        object.__setattr__(self, "version", _required(self.version, "version"))
        object.__setattr__(
            self,
            "output_policy",
            _required(self.output_policy, "output_policy"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "version": self.version,
            "harness_runtime": self.harness_runtime.to_public_dict(),
            "inference_endpoint": self.inference_endpoint.to_public_dict(),
            "credential_ref": self.credential_ref.to_public_dict(),
            "launcher_adapter": self.launcher_adapter.to_public_dict(),
            "role_policy": self.role_policy.to_public_dict(),
            "output_policy": self.output_policy,
            "raw_credential_material_exposed": False,
        }


@dataclass(frozen=True)
class ResolutionCandidate:
    value: str
    source: str
    precedence: int
    selected: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "precedence": self.precedence,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class FieldResolution:
    field_name: str
    value: str
    source: str
    precedence: int
    candidates: tuple[ResolutionCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "precedence": self.precedence,
            "candidates": [candidate.to_public_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class ResolvedAgentConfig:
    profile_id: str
    profile_version: str
    runtime_id: str
    runtime_version: str
    endpoint_id: str
    endpoint_version: str
    credential_ref: str
    credential_ref_version: str
    launcher_id: str
    launcher_version: str
    role_policy_id: str
    role_policy_version: str
    provider: str
    model: str
    backend_mode: str
    auth_mode: str
    output_policy: str
    project_id: str
    role: str
    resolutions: tuple[FieldResolution, ...]
    schema_version: str = field(default=RESOLVED_CONFIG_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolutions", tuple(self.resolutions))
        names = tuple(resolution.field_name for resolution in self.resolutions)
        if len(names) != len(set(names)):
            raise ValueError("configuration field resolutions must be unique")
        if set(names) != set(PUBLIC_CONFIGURATION_FIELDS):
            missing = sorted(set(PUBLIC_CONFIGURATION_FIELDS) - set(names))
            extra = sorted(set(names) - set(PUBLIC_CONFIGURATION_FIELDS))
            raise ValueError(
                "configuration resolution coverage mismatch: missing={}, extra={}".format(
                    missing,
                    extra,
                )
            )

    def resolution_for(self, field_name: str) -> FieldResolution:
        for resolution in self.resolutions:
            if resolution.field_name == field_name:
                return resolution
        raise KeyError(field_name)

    def to_public_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            **{field_name: getattr(self, field_name) for field_name in PUBLIC_CONFIGURATION_FIELDS},
            "resolution": {
                resolution.field_name: resolution.to_public_dict()
                for resolution in self.resolutions
            },
            "raw_credential_material_exposed": False,
        }
        return result


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    config: ResolvedAgentConfig
    profile: AgentProfile | None = None
    governance_refs: tuple[GovernanceRef, ...] = ()
    created_at: str = ""
    parent_run_id: str = ""
    successor_of_run_id: str = ""
    schema_version: str = field(default=AGENT_RUN_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "governance_refs",
            _governance_refs(self.governance_refs),
        )
        object.__setattr__(self, "created_at", str(self.created_at or "").strip())
        object.__setattr__(
            self,
            "parent_run_id",
            str(self.parent_run_id or "").strip(),
        )
        object.__setattr__(
            self,
            "successor_of_run_id",
            str(self.successor_of_run_id or "").strip(),
        )
        if self.profile and self.profile.profile_id != self.config.profile_id:
            raise ValueError("run profile does not match resolved profile_id")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "parent_run_id": self.parent_run_id,
            "successor_of_run_id": self.successor_of_run_id,
            "profile": self.profile.to_public_dict() if self.profile else None,
            "config": self.config.to_public_dict(),
            "governance_refs": {
                ref.name: ref.value for ref in self.governance_refs
            },
            "raw_credential_material_exposed": False,
        }
