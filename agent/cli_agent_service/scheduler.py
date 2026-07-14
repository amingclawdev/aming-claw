"""Deterministic, operational-only scheduling across immutable agent profiles."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Iterable, Mapping

from .certification import (
    CertificationCatalog,
    LocalModelCertification,
    RoleEligibility,
    certification_role,
    is_local_endpoint,
)
from .config import resolve_agent_config
from .models import (
    AgentProfile,
    ProfileEvaluation,
    ProfileRequirements,
    ProfileSelection,
    ProfileState,
    ScheduledAgentRun,
)
from .registry import (
    AgentRegistry,
    LeaseConflictError,
    RegistryError,
    RunRegistrationConflictError,
)


class SchedulerError(RuntimeError):
    pass


class NoEligibleProfileError(SchedulerError):
    def __init__(self, evaluations: Iterable[ProfileEvaluation]) -> None:
        self.evaluations = tuple(evaluations)
        detail = ", ".join(
            "{}:{}".format(
                item.profile_id,
                "+".join(item.rejection_reasons) or item.state,
            )
            for item in self.evaluations
        )
        super().__init__("no eligible agent profile{}".format(": " + detail if detail else ""))


class RunIdentityConflictError(SchedulerError):
    pass


_FORBIDDEN_REQUIREMENT_KEYS = {
    "api_key",
    "credential",
    "credential_value",
    "password",
    "private_prompt",
    "prompt",
    "prompt_body",
    "raw_output",
    "refresh_token",
    "route_token",
    "secret",
    "session_token",
}
_DYNAMIC_REJECTION_REASONS = {
    "auth_required",
    "cooldown_active",
    "disabled",
    "health_unavailable",
    "lease_capacity_exhausted",
    "profile_busy",
    "quota_exhausted",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Mapping):
        values = tuple(key for key, enabled in value.items() if enabled)
    else:
        values = tuple(value)
    return tuple(dict.fromkeys(_text(item) for item in values if _text(item)))


def _mapping_requirements(
    value: ProfileRequirements | Mapping[str, Any] | None,
    *,
    role: str = "",
    project_id: str = "",
) -> ProfileRequirements:
    if isinstance(value, ProfileRequirements):
        result = value
    else:
        data = dict(value or {})
        forbidden = sorted(
            key
            for key in data
            if str(key).strip().lower() in _FORBIDDEN_REQUIREMENT_KEYS
        )
        if forbidden:
            raise SchedulerError(
                "private or credential-bearing scheduling fields are forbidden: {}".format(
                    ", ".join(forbidden)
                )
            )
        exclusions = data.get("excluded_profile_ids")
        if exclusions is None:
            exclusions = data.get("exclude_profile_ids")
        if exclusions is None:
            exclusions = data.get("excluded_profiles")
        if exclusions is None:
            raw_exclusions = data.get("exclusions")
            if isinstance(raw_exclusions, Mapping):
                exclusions = (
                    raw_exclusions.get("profile_ids")
                    or raw_exclusions.get("profiles")
                    or raw_exclusions
                )
            else:
                exclusions = raw_exclusions
        result = ProfileRequirements(
            profile_id=_text(data.get("profile_id")),
            harness=_text(data.get("harness") or data.get("harness_kind")),
            runtime_id=_text(data.get("runtime_id") or data.get("runtime")),
            runtime_version=_text(data.get("runtime_version")),
            provider=_text(data.get("provider")),
            endpoint_id=_text(data.get("endpoint_id") or data.get("endpoint")),
            endpoint_version=_text(data.get("endpoint_version")),
            model=_text(data.get("model")),
            backend_mode=_text(data.get("backend_mode")),
            auth_mode=_text(data.get("auth_mode")),
            role=_text(data.get("role") or role).lower(),
            project_id=_text(data.get("project_id") or project_id),
            privacy_mode=_text(data.get("privacy_mode") or data.get("privacy")).lower(),
            output_policy=_text(data.get("output_policy")),
            required_capabilities=_values(data.get("required_capabilities")),
            excluded_profile_ids=_values(exclusions),
            preferred_profile_ids=_values(
                data.get("preferred_profile_ids") or data.get("preferred_profiles")
            ),
        )
    if role and result.role and result.role.lower() != role.lower():
        raise SchedulerError("profile requirements role conflicts with the run request")
    if project_id and result.project_id and result.project_id != project_id:
        raise SchedulerError("profile requirements project_id conflicts with the run request")
    if role and not result.role:
        result = replace(result, role=role.lower())
    if project_id and not result.project_id:
        result = replace(result, project_id=project_id)
    return result


def _normalized(value: str) -> str:
    return _text(value).lower().replace("-", "_")


def _matches_harness(profile: AgentProfile, requested: str) -> bool:
    needle = _normalized(requested)
    if not needle:
        return True
    runtime = profile.harness_runtime
    candidates = {
        _normalized(runtime.runtime_id),
        _normalized(runtime.kind),
    }
    expanded = set(candidates)
    for item in candidates:
        expanded.add(item.removeprefix("runtime_"))
        expanded.add(item.removesuffix("_cli"))
        expanded.add(item.removesuffix("_runtime"))
    return needle in expanded


class AgentScheduler:
    """Select and reserve host profiles without deciding governance progression."""

    def __init__(
        self,
        registry: AgentRegistry,
        certification_catalog: CertificationCatalog
        | Iterable[LocalModelCertification]
        | Mapping[Any, LocalModelCertification]
        | LocalModelCertification
        | None = None,
        *,
        certifications: CertificationCatalog
        | Iterable[LocalModelCertification]
        | Mapping[Any, LocalModelCertification]
        | LocalModelCertification
        | None = None,
    ) -> None:
        self.registry = registry
        if certification_catalog is not None and certifications is not None:
            raise SchedulerError(
                "provide certification_catalog or certifications, not both"
            )
        source = certifications if certifications is not None else certification_catalog
        if isinstance(source, CertificationCatalog):
            self.certifications = source
        elif isinstance(source, LocalModelCertification):
            self.certifications = CertificationCatalog((source,))
        else:
            self.certifications = CertificationCatalog(source or ())

    def certification_eligibility(
        self,
        profile: AgentProfile,
        role: str,
        *,
        now: datetime | str | None = None,
    ) -> RoleEligibility | None:
        """Return the exact-scope role assessment for a certifiable endpoint."""

        if not is_local_endpoint(profile.inference_endpoint):
            return None
        record = self.certifications.resolve_profile(profile)
        if record is None:
            return None
        return record.role_eligibility(certification_role(role), now=now)

    def _certification_rejection_reasons(
        self,
        profile: AgentProfile,
        role: str,
        *,
        now: datetime | str | None = None,
    ) -> tuple[str, ...]:
        if not is_local_endpoint(profile.inference_endpoint):
            return ()
        if not _text(role):
            return ("certification_role_required",)
        eligibility = self.certification_eligibility(profile, role, now=now)
        if eligibility is None:
            return ("certification_missing", "role_not_certified")
        if eligibility.eligible:
            return ()
        details = tuple(
            "certification_{}_missing".format(capability)
            for capability in eligibility.missing_capabilities
        ) + tuple(
            "certification_{}_failed".format(capability)
            for capability in eligibility.failed_capabilities
        ) + tuple(
            "certification_{}_revoked".format(capability)
            for capability in eligibility.revoked_capabilities
        )
        return tuple(dict.fromkeys(("role_not_certified", *details)))

    @staticmethod
    def _profile_order(
        profiles: Iterable[AgentProfile],
        requirements: ProfileRequirements,
    ) -> tuple[AgentProfile, ...]:
        preferred = {
            profile_id: index
            for index, profile_id in enumerate(requirements.preferred_profile_ids)
        }
        fallback = len(preferred)
        return tuple(
            sorted(
                profiles,
                key=lambda profile: (
                    preferred.get(profile.profile_id, fallback),
                    profile.profile_id,
                ),
            )
        )

    def _evaluate_profile(
        self,
        profile: AgentProfile,
        requirements: ProfileRequirements,
        *,
        now: datetime | str | None = None,
    ) -> ProfileEvaluation:
        capacity = self.registry.profile_capacity(profile.profile_id, now=now)
        state = capacity["profile_state"]
        endpoint = profile.inference_endpoint
        policy = profile.role_policy
        reasons: list[str] = []

        if profile.profile_id in requirements.excluded_profile_ids:
            reasons.append("profile_excluded")
        if requirements.profile_id and profile.profile_id != requirements.profile_id:
            reasons.append("profile_id_mismatch")
        if not _matches_harness(profile, requirements.harness):
            reasons.append("harness_mismatch")
        if requirements.runtime_id and profile.harness_runtime.runtime_id != requirements.runtime_id:
            reasons.append("runtime_mismatch")
        if (
            requirements.runtime_version
            and profile.harness_runtime.version != requirements.runtime_version
        ):
            reasons.append("runtime_version_mismatch")
        if requirements.provider and endpoint.provider != requirements.provider:
            reasons.append("provider_mismatch")
        if requirements.endpoint_id and endpoint.endpoint_id != requirements.endpoint_id:
            reasons.append("endpoint_mismatch")
        if requirements.endpoint_version and endpoint.version != requirements.endpoint_version:
            reasons.append("endpoint_version_mismatch")
        if requirements.model and endpoint.model != requirements.model:
            reasons.append("model_mismatch")
        if requirements.backend_mode and endpoint.backend_mode != requirements.backend_mode:
            reasons.append("backend_mode_mismatch")
        if requirements.auth_mode and endpoint.auth_mode != requirements.auth_mode:
            reasons.append("auth_mode_mismatch")
        if requirements.role and policy.roles and requirements.role not in policy.roles:
            reasons.append("role_not_allowed")
        if (
            requirements.project_id
            and policy.project_ids
            and requirements.project_id not in policy.project_ids
        ):
            reasons.append("project_not_allowed")
        if requirements.privacy_mode and profile.privacy_mode != requirements.privacy_mode:
            reasons.append("privacy_mismatch")
        if requirements.output_policy and profile.output_policy != requirements.output_policy:
            reasons.append("output_policy_mismatch")
        missing_capabilities = sorted(
            set(requirements.required_capabilities)
            - set(profile.harness_runtime.capabilities)
        )
        if missing_capabilities:
            reasons.append("required_capabilities_missing")
        reasons.extend(
            self._certification_rejection_reasons(
                profile,
                requirements.role,
                now=now,
            )
        )

        if state.state == ProfileState.BUSY.value:
            reasons.append("profile_busy")
        elif state.state == ProfileState.COOLING_DOWN.value:
            reasons.append("cooldown_active")
        elif state.state == ProfileState.QUOTA_EXHAUSTED.value:
            reasons.append("quota_exhausted")
        elif state.state == ProfileState.AUTH_REQUIRED.value:
            reasons.append("auth_required")
        elif state.state == ProfileState.UNHEALTHY.value:
            reasons.append("health_unavailable")
        elif state.state == ProfileState.DISABLED.value:
            reasons.append("disabled")
        if capacity["active_lease_count"] >= capacity["max_concurrency"]:
            reasons.append("lease_capacity_exhausted")

        return ProfileEvaluation(
            profile_id=profile.profile_id,
            state=state.state,
            eligible=not reasons,
            rejection_reasons=tuple(reasons),
            active_lease_count=capacity["active_lease_count"],
            max_concurrency=capacity["max_concurrency"],
        )

    def evaluate_profiles(
        self,
        requirements: ProfileRequirements | Mapping[str, Any] | None = None,
        *,
        role: str = "",
        project_id: str = "",
        now: datetime | str | None = None,
    ) -> tuple[ProfileEvaluation, ...]:
        resolved = _mapping_requirements(
            requirements,
            role=role,
            project_id=project_id,
        )
        profiles = self._profile_order(self.registry.list_profiles(), resolved)
        return tuple(
            self._evaluate_profile(profile, resolved, now=now) for profile in profiles
        )

    def select_profile(
        self,
        requirements: ProfileRequirements | Mapping[str, Any] | None = None,
        *,
        role: str = "",
        project_id: str = "",
        now: datetime | str | None = None,
    ) -> ProfileSelection:
        evaluations = self.evaluate_profiles(
            requirements,
            role=role,
            project_id=project_id,
            now=now,
        )
        selected = next((item for item in evaluations if item.eligible), None)
        if selected is None:
            raise NoEligibleProfileError(evaluations)
        return ProfileSelection(
            profile_id=selected.profile_id,
            selection_reason="deterministic_eligible_profile",
            evaluation=selected,
            evaluations=evaluations,
        )

    def try_select_profile(self, *args: Any, **kwargs: Any) -> ProfileSelection | None:
        try:
            return self.select_profile(*args, **kwargs)
        except NoEligibleProfileError:
            return None

    def select_qa_profile(
        self,
        implementation_profile_id: str,
        requirements: ProfileRequirements | Mapping[str, Any] | None = None,
        *,
        implementation_principal_id: str = "",
        qa_principal_id: str = "",
        allow_same_profile_fallback: bool = False,
        project_id: str = "",
        now: datetime | str | None = None,
    ) -> ProfileSelection:
        implementation_profile_id = _text(implementation_profile_id)
        if not implementation_profile_id:
            raise ValueError("implementation_profile_id is required")
        implementation_principal_id = _text(implementation_principal_id)
        qa_principal_id = _text(qa_principal_id)
        if not implementation_principal_id or not qa_principal_id:
            raise SchedulerError(
                "implementation_principal_id and qa_principal_id are required"
            )
        if implementation_principal_id == qa_principal_id:
            raise SchedulerError("QA must use a distinct governed principal")
        principal_distinct = True
        resolved = _mapping_requirements(
            requirements,
            role="qa",
            project_id=project_id,
        )
        explicitly_excluded = set(resolved.excluded_profile_ids)
        distinct_requirements = replace(
            resolved,
            excluded_profile_ids=tuple(
                dict.fromkeys((*resolved.excluded_profile_ids, implementation_profile_id))
            ),
        )
        distinct_evaluations = self.evaluate_profiles(
            distinct_requirements,
            now=now,
        )
        selected = next((item for item in distinct_evaluations if item.eligible), None)
        if selected is not None:
            return ProfileSelection(
                profile_id=selected.profile_id,
                selection_reason="distinct_qa_profile",
                evaluation=selected,
                evaluations=distinct_evaluations,
                qa_profile_distinct=True,
                qa_principal_distinct=principal_distinct,
            )
        if allow_same_profile_fallback and implementation_profile_id not in explicitly_excluded:
            fallback_evaluations = self.evaluate_profiles(resolved, now=now)
            fallback = next(
                (
                    item
                    for item in fallback_evaluations
                    if item.profile_id == implementation_profile_id and item.eligible
                ),
                None,
            )
            if fallback is not None:
                return ProfileSelection(
                    profile_id=fallback.profile_id,
                    selection_reason="explicit_same_profile_qa_fallback",
                    evaluation=fallback,
                    evaluations=fallback_evaluations,
                    same_profile_qa_fallback=True,
                    qa_profile_distinct=False,
                    qa_principal_distinct=principal_distinct,
                    evidence_flags=("same_profile_qa_fallback_explicit",),
                )
        raise NoEligibleProfileError(distinct_evaluations)

    select_independent_qa_profile = select_qa_profile

    def _existing_run_selection(
        self,
        *,
        run_id: str,
        owner_id: str,
        role: str,
        project_id: str,
        requirements: ProfileRequirements,
        ttl_seconds: int,
        now: datetime | str | None,
    ) -> ScheduledAgentRun | None:
        existing = self.registry.get_run(run_id)
        if existing is None:
            return None
        if existing.run.config.role != role or existing.run.config.project_id != project_id:
            raise RunIdentityConflictError("existing run authority scope cannot be rewritten")
        profile = existing.run.profile
        assert profile is not None
        evaluation = self._evaluate_profile(profile, requirements, now=now)
        # Capacity evaluation expires stale leases transactionally. Re-read the
        # run before deciding whether its existing lease can be retained.
        refreshed = self.registry.get_run(run_id)
        assert refreshed is not None
        existing = refreshed
        static_reasons = tuple(
            reason
            for reason in evaluation.rejection_reasons
            if reason not in _DYNAMIC_REJECTION_REASONS
        )
        if static_reasons:
            raise RunIdentityConflictError(
                "existing run profile cannot be rotated: {}".format(
                    ", ".join(static_reasons)
                )
            )
        if existing.lease is not None:
            if existing.lease.owner_id != owner_id:
                raise LeaseConflictError("existing run lease is owned by another principal")
            lease = existing.lease
        else:
            lease = self.registry.acquire_lease(
                run_id,
                owner_id,
                ttl_seconds=ttl_seconds,
                now=now,
            )
        retained = replace(
            evaluation,
            eligible=True,
            rejection_reasons=(),
        )
        selection = ProfileSelection(
            profile_id=profile.profile_id,
            selection_reason="existing_run_identity_retained",
            evaluation=retained,
            evaluations=(retained,),
            evidence_flags=("live_run_profile_identity_immutable",),
        )
        return ScheduledAgentRun(existing.run, lease, selection)

    def schedule_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        role: str,
        project_id: str,
        requirements: ProfileRequirements | Mapping[str, Any] | None = None,
        profile_requirements: ProfileRequirements | Mapping[str, Any] | None = None,
        ttl_seconds: int = 60,
        pipeline_config: Mapping[str, Any] | None = None,
        project_config: Any = None,
        compatibility_defaults: Mapping[str, Any] | None = None,
        request_overrides: Mapping[str, Any] | None = None,
        governance_refs: Mapping[str, str] | None = None,
        evidence_refs: Mapping[str, str] | None = None,
        created_at: str = "",
        parent_run_id: str = "",
        successor_of_run_id: str = "",
        require_new_run: bool = False,
        now: datetime | str | None = None,
    ) -> ScheduledAgentRun:
        run_id = _text(run_id)
        owner_id = _text(owner_id)
        role = _text(role).lower()
        project_id = _text(project_id)
        if not run_id or not owner_id or not role or not project_id:
            raise ValueError("run_id, owner_id, role, and project_id are required")
        if requirements is not None and profile_requirements is not None:
            raise ValueError("pass requirements or profile_requirements, not both")
        resolved_requirements = _mapping_requirements(
            profile_requirements if profile_requirements is not None else requirements,
            role=role,
            project_id=project_id,
        )
        if require_new_run:
            if self.registry.get_run(run_id) is not None:
                raise RunIdentityConflictError("run is already registered")
        else:
            existing = self._existing_run_selection(
                run_id=run_id,
                owner_id=owner_id,
                role=role,
                project_id=project_id,
                requirements=resolved_requirements,
                ttl_seconds=ttl_seconds,
                now=now,
            )
            if existing is not None:
                return existing

        evaluations = self.evaluate_profiles(resolved_requirements, now=now)
        for evaluation in evaluations:
            if not evaluation.eligible:
                continue
            profile = self.registry.get_profile(evaluation.profile_id)
            assert profile is not None
            run = resolve_agent_config(
                run_id=run_id,
                role=role,
                project_id=project_id,
                profile=profile,
                pipeline_config=pipeline_config,
                project_config=project_config,
                compatibility_defaults=compatibility_defaults,
                request_overrides=request_overrides,
                governance_refs=governance_refs,
                created_at=created_at,
                parent_run_id=parent_run_id,
                successor_of_run_id=successor_of_run_id,
            )
            try:
                lease = self.registry.register_run_and_acquire_lease(
                    run,
                    owner_id,
                    ttl_seconds=ttl_seconds,
                    evidence_refs=evidence_refs,
                    require_new_run=require_new_run,
                    now=now,
                )
            except RunRegistrationConflictError as exc:
                raise RunIdentityConflictError("run is already registered") from exc
            except LeaseConflictError:
                continue
            selection = ProfileSelection(
                profile_id=profile.profile_id,
                selection_reason="deterministic_eligible_profile_with_atomic_lease",
                evaluation=evaluation,
                evaluations=evaluations,
            )
            return ScheduledAgentRun(run, lease, selection)
        raise NoEligibleProfileError(
            self.evaluate_profiles(resolved_requirements, now=now)
        )

    schedule = schedule_run

    def record_profile_outcome(
        self,
        profile_id: str,
        outcome: str,
        *,
        run_id: str = "",
        reason_code: str = "",
        retry_at: datetime | str | None = None,
        cooldown_seconds: int | None = None,
        now: datetime | str | None = None,
    ):
        if run_id:
            run = self.registry.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run.run.config.profile_id != profile_id:
                raise RunIdentityConflictError("outcome profile does not match immutable run identity")
        return self.registry.record_profile_signal(
            profile_id,
            outcome,
            reason_code=reason_code,
            retry_at=retry_at,
            cooldown_seconds=cooldown_seconds,
            now=now,
        )


MultiProfileScheduler = AgentScheduler
CliAgentScheduler = AgentScheduler
ProfileScheduler = AgentScheduler
SchedulingError = SchedulerError
NoAvailableProfileError = NoEligibleProfileError
