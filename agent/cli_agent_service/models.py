"""Immutable, public-safe configuration models for the CLI Agent Service."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
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
REGISTRY_LEASE_SCHEMA_VERSION = "cli_agent_service.registry_lease.v1"
REGISTRY_RUN_SCHEMA_VERSION = "cli_agent_service.registry_run.v1"
RECONCILIATION_SCHEMA_VERSION = "cli_agent_service.reconciliation.v1"
PROFILE_STATE_SCHEMA_VERSION = "cli_agent_service.profile_state.v1"
PROFILE_REQUIREMENTS_SCHEMA_VERSION = "cli_agent_service.profile_requirements.v1"
PROFILE_EVALUATION_SCHEMA_VERSION = "cli_agent_service.profile_evaluation.v1"
PROFILE_SELECTION_SCHEMA_VERSION = "cli_agent_service.profile_selection.v1"
SCHEDULED_AGENT_RUN_SCHEMA_VERSION = "cli_agent_service.scheduled_run.v1"

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
_CREDENTIAL_REF_PATTERN = re.compile(
    r"(?:credential:[a-z0-9][a-z0-9_-]{1,63}"
    r"(?::[a-z0-9][a-z0-9_-]{1,63})+|credref-[a-z0-9][a-z0-9-]{2,127})"
)
_SECRET_SHAPED_CREDENTIAL_SEGMENT = re.compile(
    r"(?:^|[-_])(api[-_]?key|bearer|password|raw|secret|token)(?:$|[-_])|"
    r"^(?:sk|ghp|github_pat|xox[baprs])[-_]",
    re.IGNORECASE,
)
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
    "snapshot_id": re.compile(r"[a-z][a-z0-9-]{2,191}"),
    "timeline_ref": re.compile(r"timeline:[0-9]+"),
    "commit_sha": re.compile(r"[0-9a-f]{40}"),
}


def validate_credential_ref_id(value: str) -> str:
    """Return one opaque credential reference or reject secret-shaped material."""
    normalized = _required(value, "ref_id")
    if not _CREDENTIAL_REF_PATTERN.fullmatch(normalized):
        raise ValueError("credential reference must be an opaque public-safe identifier")
    segments = normalized.replace("credref-", "credential:", 1).split(":")[1:]
    if any(_SECRET_SHAPED_CREDENTIAL_SEGMENT.search(segment) for segment in segments):
        raise ValueError("credential reference must not contain credential material")
    return normalized


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
        object.__setattr__(self, "ref_id", validate_credential_ref_id(self.ref_id))
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
    cooldown_sec: int = 0
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
        if self.cooldown_sec < 0:
            raise ValueError("cooldown_sec cannot be negative")
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
            "cooldown_sec": self.cooldown_sec,
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
    privacy_mode: str = "host_private"
    schema_version: str = field(default=AGENT_PROFILE_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _required(self.profile_id, "profile_id"))
        object.__setattr__(self, "version", _required(self.version, "version"))
        object.__setattr__(
            self,
            "output_policy",
            _required(self.output_policy, "output_policy"),
        )
        object.__setattr__(
            self,
            "privacy_mode",
            _required(self.privacy_mode, "privacy_mode").lower(),
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
            "privacy_mode": self.privacy_mode,
            "raw_credential_material_exposed": False,
        }


@dataclass(frozen=True)
class ProfileRequirements:
    """Public scheduling constraints supplied by an authorized run request."""

    profile_id: str = ""
    harness: str = ""
    runtime_id: str = ""
    runtime_version: str = ""
    provider: str = ""
    endpoint_id: str = ""
    endpoint_version: str = ""
    model: str = ""
    backend_mode: str = ""
    auth_mode: str = ""
    role: str = ""
    project_id: str = ""
    privacy_mode: str = ""
    output_policy: str = ""
    required_capabilities: tuple[str, ...] = ()
    excluded_profile_ids: tuple[str, ...] = ()
    preferred_profile_ids: tuple[str, ...] = ()
    schema_version: str = field(
        default=PROFILE_REQUIREMENTS_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "profile_id",
            "harness",
            "runtime_id",
            "runtime_version",
            "provider",
            "endpoint_id",
            "endpoint_version",
            "model",
            "backend_mode",
            "auth_mode",
            "role",
            "project_id",
            "privacy_mode",
            "output_policy",
        ):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        for name in (
            "required_capabilities",
            "excluded_profile_ids",
            "preferred_profile_ids",
        ):
            values = tuple(dict.fromkeys(_strings(getattr(self, name))))
            object.__setattr__(self, name, values)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "harness": self.harness,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "provider": self.provider,
            "endpoint_id": self.endpoint_id,
            "endpoint_version": self.endpoint_version,
            "model": self.model,
            "backend_mode": self.backend_mode,
            "auth_mode": self.auth_mode,
            "role": self.role,
            "project_id": self.project_id,
            "privacy_mode": self.privacy_mode,
            "output_policy": self.output_policy,
            "required_capabilities": list(self.required_capabilities),
            "excluded_profile_ids": list(self.excluded_profile_ids),
            "preferred_profile_ids": list(self.preferred_profile_ids),
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


class RunState(str, Enum):
    """Host execution states; these values never imply governance progress."""

    REGISTERED = "registered"
    LEASED = "leased"
    RUNNING = "running"
    LIVE = "live"
    COMPLETED = "completed"
    FAILED = "failed"
    LOST = "lost"
    ORPHANED = "orphaned"


class ProfileState(str, Enum):
    """Host-private availability states used only for future scheduling."""

    READY = "ready"
    BUSY = "busy"
    COOLING_DOWN = "cooling_down"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_REQUIRED = "auth_required"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


PROFILE_STATES = tuple(item.value for item in ProfileState)


@dataclass(frozen=True)
class ProfileStateRecord:
    profile_id: str
    state: str
    reason_code: str = ""
    cooldown_until: str = ""
    quota_reset_at: str = ""
    consecutive_crashes: int = 0
    updated_at: str = ""
    schema_version: str = field(default=PROFILE_STATE_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        profile_id = _required(self.profile_id, "profile_id")
        state = str(self.state or "").strip().lower()
        if state not in PROFILE_STATES:
            raise ValueError("invalid profile state: {}".format(self.state))
        if self.consecutive_crashes < 0:
            raise ValueError("consecutive_crashes cannot be negative")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason_code", str(self.reason_code or "").strip())
        object.__setattr__(self, "cooldown_until", str(self.cooldown_until or "").strip())
        object.__setattr__(self, "quota_reset_at", str(self.quota_reset_at or "").strip())
        object.__setattr__(self, "updated_at", str(self.updated_at or "").strip())

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "state": self.state,
            "reason_code": self.reason_code,
            "cooldown_until": self.cooldown_until,
            "quota_reset_at": self.quota_reset_at,
            "consecutive_crashes": self.consecutive_crashes,
            "updated_at": self.updated_at,
            "operational_state_only": True,
            "governance_authority": False,
            "raw_provider_output_persisted": False,
        }


@dataclass(frozen=True)
class ProfileEvaluation:
    profile_id: str
    state: str
    eligible: bool
    rejection_reasons: tuple[str, ...] = ()
    active_lease_count: int = 0
    max_concurrency: int = 1
    schema_version: str = field(default=PROFILE_EVALUATION_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _required(self.profile_id, "profile_id"))
        object.__setattr__(self, "state", str(self.state or "").strip().lower())
        object.__setattr__(
            self,
            "rejection_reasons",
            tuple(dict.fromkeys(_strings(self.rejection_reasons))),
        )
        if self.active_lease_count < 0 or self.max_concurrency < 1:
            raise ValueError("profile lease counts are invalid")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "state": self.state,
            "eligible": self.eligible,
            "rejection_reasons": list(self.rejection_reasons),
            "active_lease_count": self.active_lease_count,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True)
class ProfileSelection:
    profile_id: str
    selection_reason: str
    evaluation: ProfileEvaluation
    evaluations: tuple[ProfileEvaluation, ...] = ()
    same_profile_qa_fallback: bool = False
    qa_profile_distinct: bool | None = None
    qa_principal_distinct: bool | None = None
    evidence_flags: tuple[str, ...] = ()
    schema_version: str = field(default=PROFILE_SELECTION_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _required(self.profile_id, "profile_id"))
        object.__setattr__(
            self,
            "selection_reason",
            _required(self.selection_reason, "selection_reason"),
        )
        object.__setattr__(self, "evaluations", tuple(self.evaluations))
        object.__setattr__(
            self,
            "evidence_flags",
            tuple(dict.fromkeys(_strings(self.evidence_flags))),
        )
        if self.evaluation.profile_id != self.profile_id:
            raise ValueError("selected profile does not match its evaluation")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "selection_reason": self.selection_reason,
            "evaluation": self.evaluation.to_public_dict(),
            "evaluations": [item.to_public_dict() for item in self.evaluations],
            "same_profile_qa_fallback": self.same_profile_qa_fallback,
            "qa_profile_distinct": self.qa_profile_distinct,
            "qa_principal_distinct": self.qa_principal_distinct,
            "evidence_flags": list(self.evidence_flags),
            "operational_state_only": True,
            "governance_authority": False,
        }


@dataclass(frozen=True)
class RegistryLease:
    lease_id: str
    run_id: str
    profile_id: str
    owner_id: str
    status: str
    acquired_at: str
    expires_at: str
    heartbeat_at: str = ""
    released_at: str = ""
    schema_version: str = field(default=REGISTRY_LEASE_SCHEMA_VERSION, init=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "profile_id": self.profile_id,
            "owner_id": self.owner_id,
            "status": self.status,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "heartbeat_at": self.heartbeat_at,
            "released_at": self.released_at,
        }


@dataclass(frozen=True)
class RegistryRun:
    run: AgentRun
    state: str
    pid: int | None = None
    process_start_identity: str = ""
    process_group_id: int | None = None
    argv_hash: str = ""
    last_heartbeat_at: str = ""
    lease: RegistryLease | None = None
    evidence_refs: tuple[GovernanceRef, ...] = ()
    exit_code: int | None = None
    failure_category: str = ""
    updated_at: str = ""
    schema_version: str = field(default=REGISTRY_RUN_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        normalized_state = str(self.state or "").strip().lower()
        if normalized_state not in {item.value for item in RunState}:
            raise ValueError("invalid registry run state: {}".format(self.state))
        object.__setattr__(self, "state", normalized_state)
        object.__setattr__(self, "evidence_refs", _governance_refs(self.evidence_refs))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run": self.run.to_public_dict(),
            "state": self.state,
            "pid": self.pid,
            "process_start_identity": self.process_start_identity,
            "process_group_id": self.process_group_id,
            "argv_hash": self.argv_hash,
            "last_heartbeat_at": self.last_heartbeat_at,
            "lease": self.lease.to_public_dict() if self.lease else None,
            "evidence_refs": {ref.name: ref.value for ref in self.evidence_refs},
            "exit_code": self.exit_code,
            "failure_category": self.failure_category,
            "updated_at": self.updated_at,
            "operational_state_only": True,
            "governance_authority": False,
        }


@dataclass(frozen=True)
class ScheduledAgentRun:
    run: AgentRun
    lease: RegistryLease
    selection: ProfileSelection
    schema_version: str = field(default=SCHEDULED_AGENT_RUN_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if self.run.run_id != self.lease.run_id:
            raise ValueError("scheduled run and lease identities do not match")
        if self.run.config.profile_id != self.selection.profile_id:
            raise ValueError("scheduled run and profile selection do not match")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run": self.run.to_public_dict(),
            "lease": self.lease.to_public_dict(),
            "selection": self.selection.to_public_dict(),
            "operational_state_only": True,
            "governance_authority": False,
        }


@dataclass(frozen=True)
class ProcessObservation:
    alive: bool
    start_identity: str = ""
    exit_code: int | None = None
    observable: bool = True


@dataclass(frozen=True)
class ReconciliationResult:
    run_id: str
    classification: str
    previous_state: str
    process_identity_match: bool = False
    detail: str = ""
    schema_version: str = field(default=RECONCILIATION_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if self.classification not in {
            RunState.LIVE.value,
            RunState.COMPLETED.value,
            RunState.FAILED.value,
            RunState.LOST.value,
            RunState.ORPHANED.value,
        }:
            raise ValueError("invalid reconciliation classification")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "classification": self.classification,
            "previous_state": self.previous_state,
            "process_identity_match": self.process_identity_match,
            "detail": self.detail,
            "operational_state_only": True,
            "governance_authority": False,
        }


# Descriptive aliases retained for callers that name the persisted shape explicitly.
RegistryRunRecord = RegistryRun
LeaseState = RegistryLease
ProfileAvailabilityState = ProfileState
AgentProfileState = ProfileStateRecord
ProfileRuntimeState = ProfileStateRecord
SchedulingRequirements = ProfileRequirements
SchedulingDecision = ProfileSelection
