"""Public-safe capability certification for local and compatible endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Iterable, Mapping

from .models import AgentProfile, InferenceEndpoint


CERTIFICATION_SCOPE_SCHEMA_VERSION = "cli_agent_service.certification_scope.v1"
CAPABILITY_RESULT_SCHEMA_VERSION = "cli_agent_service.capability_result.v1"
ROLE_ELIGIBILITY_SCHEMA_VERSION = "cli_agent_service.role_eligibility.v1"
CERTIFICATION_SCHEMA_VERSION = "cli_agent_service.local_model_certification.v1"


class CertificationCapability(str, Enum):
    """Independently probed endpoint behavior; discovery is never a role grant."""

    HEALTH = "health"
    MODEL_DISCOVERY = "model_discovery"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    READ_ONLY_TOOLS = "read_only_tools"
    AMING_CLAW_MCP = "aming_claw_mcp"
    RUNTIME_CONTEXT = "runtime_context"
    READ_RECEIPT = "read_receipt"
    ISOLATED_WORKTREE_EDIT = "isolated_worktree_edit"
    ISOLATED_WORKTREE_TEST = "isolated_worktree_test"
    HEARTBEAT = "heartbeat"
    TIMEOUT = "timeout"
    CANCEL = "cancel"
    RESUME = "resume"
    CONTEXT_LIMITS = "context_limits"
    RELIABILITY = "reliability"


class CapabilityStatus(str, Enum):
    UNTESTED = "untested"
    PASSED = "passed"
    FAILED = "failed"
    REVOKED = "revoked"


Capability = CertificationCapability
ProbeStatus = CapabilityStatus


_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{1,95}")
_SAFE_EVIDENCE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/@+-]{1,511}")


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("{} is required".format(field_name))
    return normalized


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _timestamp(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("certification timestamp is invalid") from exc
    return (
        parsed.astimezone(timezone.utc)
        if parsed.tzinfo
        else parsed.replace(tzinfo=timezone.utc)
    )


def _safe_code(value: Any, field_name: str) -> str:
    normalized = _normalized(value)
    if normalized and not _SAFE_CODE.fullmatch(normalized):
        raise ValueError("{} must be a public-safe reason code".format(field_name))
    return normalized


def _safe_evidence_ref(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized and not _SAFE_EVIDENCE_REF.fullmatch(normalized):
        raise ValueError("evidence_ref must be a public-safe reference")
    lowered = normalized.lower().replace("-", "_")
    if normalized and (
        lowered.startswith(("credential:", "credref_"))
        or any(
            marker in lowered
            for marker in ("api_key", "password", "raw_secret", "session_token")
        )
    ):
        raise ValueError("evidence_ref must not contain credential material")
    return normalized


def _capability(value: CertificationCapability | str) -> CertificationCapability:
    if isinstance(value, CertificationCapability):
        return value
    try:
        return CertificationCapability(_normalized(value))
    except ValueError as exc:
        raise ValueError("unknown certification capability") from exc


def _status(value: CapabilityStatus | str | bool) -> CapabilityStatus:
    if isinstance(value, CapabilityStatus):
        return value
    if isinstance(value, bool):
        return CapabilityStatus.PASSED if value else CapabilityStatus.FAILED
    normalized = _normalized(value)
    aliases = {"pass": "passed", "ok": "passed", "fail": "failed"}
    try:
        return CapabilityStatus(aliases.get(normalized, normalized))
    except ValueError as exc:
        raise ValueError("unknown capability status") from exc


@dataclass(frozen=True, order=True)
class CertificationScope:
    """Exact immutable identity covered by one certification record."""

    runtime_id: str
    runtime_version: str
    endpoint_id: str
    endpoint_version: str
    model: str
    policy_id: str
    policy_version: str
    provider: str = ""
    schema_version: str = field(
        default=CERTIFICATION_SCOPE_SCHEMA_VERSION,
        init=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "runtime_id",
            "runtime_version",
            "endpoint_id",
            "endpoint_version",
            "model",
            "policy_id",
            "policy_version",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "provider", str(self.provider or "").strip())

    @classmethod
    def from_profile(cls, profile: AgentProfile) -> "CertificationScope":
        return cls(
            runtime_id=profile.harness_runtime.runtime_id,
            runtime_version=profile.harness_runtime.version,
            endpoint_id=profile.inference_endpoint.endpoint_id,
            endpoint_version=profile.inference_endpoint.version,
            model=profile.inference_endpoint.model,
            policy_id=profile.role_policy.policy_id,
            policy_version=profile.role_policy.version,
            provider=profile.inference_endpoint.provider,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "endpoint_id": self.endpoint_id,
            "endpoint_version": self.endpoint_version,
            "model": self.model,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "provider": self.provider,
        }


@dataclass(frozen=True, order=True)
class CapabilityResult:
    capability: CertificationCapability | str
    status: CapabilityStatus | str | bool
    reason_code: str = ""
    evidence_ref: str = ""
    observed_at: str = ""
    expires_at: str = ""
    schema_version: str = field(
        default=CAPABILITY_RESULT_SCHEMA_VERSION,
        init=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _capability(self.capability))
        object.__setattr__(self, "status", _status(self.status))
        object.__setattr__(
            self,
            "reason_code",
            _safe_code(self.reason_code, "reason_code"),
        )
        object.__setattr__(self, "evidence_ref", _safe_evidence_ref(self.evidence_ref))
        for name in ("observed_at", "expires_at"):
            normalized = str(getattr(self, name) or "").strip()
            if normalized:
                _timestamp(normalized)
            object.__setattr__(self, name, normalized)

    @property
    def passed(self) -> bool:
        return self.status == CapabilityStatus.PASSED

    def is_current(self, now: datetime | str | None = None) -> bool:
        if not self.passed:
            return False
        expiry = _timestamp(self.expires_at)
        if expiry is None:
            return True
        if isinstance(now, datetime):
            current = (
                now.astimezone(timezone.utc)
                if now.tzinfo
                else now.replace(tzinfo=timezone.utc)
            )
        elif now:
            parsed = _timestamp(str(now))
            assert parsed is not None
            current = parsed
        else:
            current = datetime.now(timezone.utc)
        return expiry > current

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability": self.capability.value,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "evidence_ref": self.evidence_ref,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "raw_probe_output_exposed": False,
        }


CapabilityCertification = CapabilityResult
ProbeResult = CapabilityResult


_UTILITY_CAPABILITIES = (
    CertificationCapability.STRUCTURED_OUTPUT,
    CertificationCapability.CONTEXT_LIMITS,
    CertificationCapability.RELIABILITY,
)
_LIFECYCLE_CAPABILITIES = (
    CertificationCapability.HEARTBEAT,
    CertificationCapability.TIMEOUT,
    CertificationCapability.CANCEL,
    CertificationCapability.RESUME,
)
ROLE_CAPABILITY_REQUIREMENTS: Mapping[str, tuple[CertificationCapability, ...]] = {
    "utility": _UTILITY_CAPABILITIES,
    "worker": (
        *_UTILITY_CAPABILITIES,
        CertificationCapability.STREAMING,
        CertificationCapability.AMING_CLAW_MCP,
        CertificationCapability.RUNTIME_CONTEXT,
        CertificationCapability.READ_RECEIPT,
        CertificationCapability.ISOLATED_WORKTREE_EDIT,
        CertificationCapability.ISOLATED_WORKTREE_TEST,
        *_LIFECYCLE_CAPABILITIES,
    ),
    "observer": (
        *_UTILITY_CAPABILITIES,
        CertificationCapability.STREAMING,
        CertificationCapability.READ_ONLY_TOOLS,
        CertificationCapability.AMING_CLAW_MCP,
        CertificationCapability.RUNTIME_CONTEXT,
        CertificationCapability.READ_RECEIPT,
        *_LIFECYCLE_CAPABILITIES,
    ),
    "qa": (
        *_UTILITY_CAPABILITIES,
        CertificationCapability.READ_ONLY_TOOLS,
        CertificationCapability.AMING_CLAW_MCP,
        CertificationCapability.RUNTIME_CONTEXT,
        CertificationCapability.READ_RECEIPT,
        CertificationCapability.ISOLATED_WORKTREE_TEST,
        *_LIFECYCLE_CAPABILITIES,
    ),
}
_ROLE_ALIASES = {
    "dev": "worker",
    "developer": "worker",
    "mf_sub": "worker",
    "mf_subagent": "worker",
    "bounded_worker": "worker",
    "reviewer": "qa",
}


def certification_role(role: str) -> str:
    normalized = _normalized(role)
    return _ROLE_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class RoleEligibility:
    role: str
    eligible: bool
    bounded: bool
    required_capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...] = ()
    failed_capabilities: tuple[str, ...] = ()
    revoked_capabilities: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    schema_version: str = field(default=ROLE_ELIGIBILITY_SCHEMA_VERSION, init=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "eligible": self.eligible,
            "bounded": self.bounded,
            "required_capabilities": list(self.required_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "failed_capabilities": list(self.failed_capabilities),
            "revoked_capabilities": list(self.revoked_capabilities),
            "reason_codes": list(self.reason_codes),
            "health_or_discovery_alone_grants_role": False,
            "governance_authority": False,
        }


def _capability_results(
    values: Iterable[CapabilityResult] | Mapping[Any, Any],
) -> tuple[CapabilityResult, ...]:
    items: list[CapabilityResult] = []
    if isinstance(values, Mapping):
        for capability, value in values.items():
            if isinstance(value, CapabilityResult):
                result = value
                if result.capability != _capability(capability):
                    raise ValueError("capability result key does not match its value")
            elif isinstance(value, Mapping):
                result = CapabilityResult(capability=capability, **dict(value))
            else:
                result = CapabilityResult(capability=capability, status=value)
            items.append(result)
    else:
        items.extend(values)
    normalized = tuple(
        item if isinstance(item, CapabilityResult) else CapabilityResult(*item)
        for item in items
    )
    names = tuple(item.capability for item in normalized)
    if len(names) != len(set(names)):
        raise ValueError("certification capabilities must be unique")
    return tuple(sorted(normalized, key=lambda item: item.capability.value))


@dataclass(frozen=True)
class LocalModelCertification:
    scope: CertificationScope
    capabilities: tuple[CapabilityResult, ...] | Mapping[Any, Any] = ()
    active: bool = True
    revocation_reason_code: str = ""
    reliability_successes: int = 0
    reliability_samples: int = 0
    schema_version: str = field(default=CERTIFICATION_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", _capability_results(self.capabilities))
        object.__setattr__(
            self,
            "revocation_reason_code",
            _safe_code(self.revocation_reason_code, "revocation_reason_code"),
        )
        if self.reliability_samples < 0 or self.reliability_successes < 0:
            raise ValueError("reliability counts cannot be negative")
        if self.reliability_successes > self.reliability_samples:
            raise ValueError("reliability successes cannot exceed samples")

    def result_for(
        self,
        capability: CertificationCapability | str,
    ) -> CapabilityResult | None:
        requested = _capability(capability)
        return next(
            (item for item in self.capabilities if item.capability == requested),
            None,
        )

    def with_result(self, result: CapabilityResult) -> "LocalModelCertification":
        replacement = result if isinstance(result, CapabilityResult) else CapabilityResult(*result)
        values = [
            item for item in self.capabilities if item.capability != replacement.capability
        ]
        values.append(replacement)
        return replace(self, capabilities=tuple(values))

    def with_capability(
        self,
        capability: CertificationCapability | str,
        status: CapabilityStatus | str | bool,
        *,
        reason_code: str = "",
        evidence_ref: str = "",
        observed_at: str = "",
        expires_at: str = "",
    ) -> "LocalModelCertification":
        return self.with_result(
            CapabilityResult(
                capability=capability,
                status=status,
                reason_code=reason_code,
                evidence_ref=evidence_ref,
                observed_at=observed_at,
                expires_at=expires_at,
            )
        )

    def revoke(self, reason_code: str = "certification_revoked") -> "LocalModelCertification":
        return replace(
            self,
            active=False,
            revocation_reason_code=_safe_code(reason_code, "reason_code"),
        )

    def record_reliability_outcome(
        self,
        success: bool,
        *,
        minimum_samples: int = 3,
        minimum_success_rate: float = 0.8,
        evidence_ref: str = "",
        observed_at: str = "",
    ) -> "LocalModelCertification":
        if minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        if not 0.0 <= minimum_success_rate <= 1.0:
            raise ValueError("minimum_success_rate must be between zero and one")
        samples = self.reliability_samples + 1
        successes = self.reliability_successes + int(bool(success))
        if samples < minimum_samples:
            status = CapabilityStatus.UNTESTED
            reason = "reliability_sample_incomplete"
        elif successes / samples >= minimum_success_rate:
            status = CapabilityStatus.PASSED
            reason = "reliability_threshold_passed"
        else:
            status = CapabilityStatus.FAILED
            reason = "reliability_degraded"
        updated = replace(
            self,
            reliability_samples=samples,
            reliability_successes=successes,
        )
        return updated.with_capability(
            CertificationCapability.RELIABILITY,
            status,
            reason_code=reason,
            evidence_ref=evidence_ref,
            observed_at=observed_at,
        )

    def role_eligibility(
        self,
        role: str,
        *,
        now: datetime | str | None = None,
    ) -> RoleEligibility:
        normalized_role = certification_role(role)
        required = ROLE_CAPABILITY_REQUIREMENTS.get(normalized_role)
        if required is None:
            return RoleEligibility(
                role=normalized_role,
                eligible=False,
                bounded=False,
                required_capabilities=(),
                reason_codes=("unsupported_role",),
            )
        missing: list[str] = []
        failed: list[str] = []
        revoked: list[str] = []
        for capability in required:
            result = self.result_for(capability)
            if result is None or result.status == CapabilityStatus.UNTESTED:
                missing.append(capability.value)
            elif result.status == CapabilityStatus.REVOKED:
                revoked.append(capability.value)
            elif result.status == CapabilityStatus.FAILED or not result.is_current(now):
                failed.append(capability.value)
        reasons: list[str] = []
        if not self.active:
            reasons.append(self.revocation_reason_code or "certification_revoked")
        if missing:
            reasons.append("capabilities_missing")
        if failed:
            reasons.append("capabilities_failed_or_expired")
        if revoked:
            reasons.append("capabilities_revoked")
        return RoleEligibility(
            role=normalized_role,
            eligible=self.active and not missing and not failed and not revoked,
            bounded=normalized_role in {"utility", "worker"},
            required_capabilities=tuple(item.value for item in required),
            missing_capabilities=tuple(missing),
            failed_capabilities=tuple(failed),
            revoked_capabilities=tuple(revoked),
            reason_codes=tuple(reasons),
        )

    assess_role = role_eligibility

    def eligible_roles(self, *, now: datetime | str | None = None) -> tuple[str, ...]:
        return tuple(
            role
            for role in ("utility", "worker", "observer", "qa")
            if self.role_eligibility(role, now=now).eligible
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.to_public_dict(),
            "active": self.active,
            "revocation_reason_code": self.revocation_reason_code,
            "capabilities": [item.to_public_dict() for item in self.capabilities],
            "role_eligibility": {
                role: self.role_eligibility(role).to_public_dict()
                for role in ("utility", "worker", "observer", "qa")
            },
            "reliability_successes": self.reliability_successes,
            "reliability_samples": self.reliability_samples,
            "health_or_model_discovery_grants_role": False,
            "raw_probe_output_exposed": False,
            "raw_credentials_exposed": False,
            "governance_authority": False,
        }


CertificationRecord = LocalModelCertification
ModelCertification = LocalModelCertification


class CertificationCatalog:
    """Exact-scope in-memory catalog; callers own persistence and probe execution."""

    def __init__(
        self,
        records: Iterable[LocalModelCertification]
        | Mapping[Any, LocalModelCertification] = (),
    ) -> None:
        self._records: dict[CertificationScope, LocalModelCertification] = {}
        values = records.values() if isinstance(records, Mapping) else records
        for record in values:
            self.upsert(record)

    def upsert(self, record: LocalModelCertification) -> LocalModelCertification:
        if not isinstance(record, LocalModelCertification):
            raise TypeError("certification catalog accepts certification records only")
        self._records[record.scope] = record
        return record

    add = upsert
    register = upsert

    def get(self, scope: CertificationScope) -> LocalModelCertification | None:
        return self._records.get(scope)

    def resolve_profile(self, profile: AgentProfile) -> LocalModelCertification | None:
        return self.get(CertificationScope.from_profile(profile))

    resolve = resolve_profile

    def revoke(
        self,
        scope: CertificationScope,
        reason_code: str = "certification_revoked",
    ) -> LocalModelCertification:
        current = self.get(scope)
        if current is None:
            raise KeyError(scope)
        return self.upsert(current.revoke(reason_code))

    def records(self) -> tuple[LocalModelCertification, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


CertificationRegistry = CertificationCatalog


_LOCAL_ENDPOINT_KINDS = {
    "claude_compatible_gateway",
    "claude_gateway",
    "codex_oss",
    "compatible_gateway",
    "local",
    "local_endpoint",
    "local_model",
}
_LOCAL_BACKEND_MODES = {
    "claude_compatible",
    "claude_gateway",
    "codex_oss",
    "compatible_gateway",
    "local",
    "local_provider",
}
_LOCAL_PROVIDERS = {
    "anthropic_compatible",
    "claude_gateway",
    "codex_oss",
    "lmstudio",
    "local",
    "ollama",
    "openai_compatible",
}


def is_local_endpoint(endpoint: InferenceEndpoint) -> bool:
    """Return whether an endpoint needs certification before role scheduling."""

    return bool(
        _normalized(endpoint.endpoint_kind) in _LOCAL_ENDPOINT_KINDS
        or _normalized(endpoint.backend_mode) in _LOCAL_BACKEND_MODES
        or _normalized(endpoint.provider) in _LOCAL_PROVIDERS
    )


requires_certification = is_local_endpoint
