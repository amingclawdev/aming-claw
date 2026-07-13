import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


NOW = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _profile(
    profile_id,
    *,
    account="primary",
    runtime_id="runtime-codex",
    harness="codex_cli",
    provider="openai",
    endpoint_id="endpoint-openai",
    model="gpt-5.4-codex",
    backend_mode="codex_cli",
    auth_mode="cli_auth",
    roles=("dev", "qa"),
    projects=("aming-claw",),
    capabilities=("worker", "qa", "tool_use"),
    max_concurrency=1,
    privacy_mode="host_private",
    cooldown_sec=30,
):
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    return AgentProfile(
        profile_id=profile_id,
        version="1",
        harness_runtime=HarnessRuntime(
            runtime_id=runtime_id,
            version="1",
            kind=harness,
            executable_ref="managed:{}".format(harness),
            capabilities=capabilities,
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id=endpoint_id,
            version="1",
            provider=provider,
            model=model,
            backend_mode=backend_mode,
            auth_mode=auth_mode,
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:{}".format(account),
            version="1",
            provider=provider,
            ref_kind="provider_home",
        ),
        launcher_adapter=LauncherAdapter(
            launcher_id="launcher-{}".format(profile_id),
            version="1",
            kind="process",
            environment_keys=("CODEX_HOME", "PROFILE_{}".format(account.upper())),
        ),
        role_policy=RolePolicy(
            policy_id="policy-{}".format(profile_id),
            version="1",
            roles=roles,
            project_ids=projects,
            max_concurrency=max_concurrency,
            timeout_sec=300,
            cooldown_sec=cooldown_sec,
        ),
        privacy_mode=privacy_mode,
    )


def _registry(tmp_path):
    from cli_agent_service.registry import AgentRegistry

    return AgentRegistry(
        tmp_path / "private" / "scheduler.db",
        clock=lambda: NOW,
    )


def _requirements(**overrides):
    values = {
        "harness": "codex",
        "runtime_id": "runtime-codex",
        "provider": "openai",
        "endpoint_id": "endpoint-openai",
        "model": "gpt-5.4-codex",
        "role": "dev",
        "project_id": "aming-claw",
        "privacy_mode": "host_private",
        "required_capabilities": ("worker", "tool_use"),
    }
    values.update(overrides)
    return values


def test_scheduler_filters_every_profile_constraint_deterministically(tmp_path):
    from cli_agent_service.scheduler import AgentScheduler

    registry = _registry(tmp_path)
    profiles = (
        _profile("profile-b", account="acct-b"),
        _profile("profile-a", account="acct-a"),
        _profile("profile-runtime", account="acct-r", runtime_id="runtime-claude"),
        _profile("profile-model", account="acct-m", model="gpt-other"),
        _profile("profile-role", account="role", roles=("qa",)),
        _profile("profile-project", account="project", projects=("other",)),
        _profile("profile-privacy", account="privacy", privacy_mode="shared"),
        _profile("profile-capability", account="cap", capabilities=("worker",)),
    )
    for profile in profiles:
        registry.register_profile(profile)

    selection = AgentScheduler(registry).select_profile(
        _requirements(excluded_profile_ids=("profile-a",))
    )

    assert selection.profile_id == "profile-b"
    evaluations = {item.profile_id: item for item in selection.evaluations}
    assert evaluations["profile-a"].rejection_reasons == ("profile_excluded",)
    assert "runtime_mismatch" in evaluations["profile-runtime"].rejection_reasons
    assert "model_mismatch" in evaluations["profile-model"].rejection_reasons
    assert "role_not_allowed" in evaluations["profile-role"].rejection_reasons
    assert "project_not_allowed" in evaluations["profile-project"].rejection_reasons
    assert "privacy_mismatch" in evaluations["profile-privacy"].rejection_reasons
    assert "required_capabilities_missing" in (
        evaluations["profile-capability"].rejection_reasons
    )


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        ("cooling_down", "cooldown_active"),
        ("quota_exhausted", "quota_exhausted"),
        ("auth_required", "auth_required"),
        ("unhealthy", "health_unavailable"),
        ("disabled", "disabled"),
    ],
)
def test_scheduler_rejects_non_ready_persistent_states(
    tmp_path,
    state,
    expected_reason,
):
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    registry.register_profile(_profile("profile-state", account="state"))
    registry.set_profile_state(
        "profile-state",
        state,
        reason_code=state,
        cooldown_until=NOW + timedelta(minutes=5) if state in {"cooling_down", "unhealthy"} else None,
        quota_reset_at=NOW + timedelta(minutes=5) if state == "quota_exhausted" else None,
    )

    with pytest.raises(NoEligibleProfileError) as denied:
        AgentScheduler(registry).select_profile(_requirements())

    assert expected_reason in denied.value.evaluations[0].rejection_reasons
    restarted = _registry(tmp_path)
    assert restarted.get_profile_state("profile-state").state == state


def test_cooldown_and_quota_deadlines_restore_future_eligibility(tmp_path):
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    registry.register_profile(_profile("profile-temporary", account="temp"))
    registry.record_quota_exhausted(
        "profile-temporary",
        retry_at=NOW + timedelta(seconds=10),
        now=NOW,
    )
    scheduler = AgentScheduler(registry)

    with pytest.raises(NoEligibleProfileError):
        scheduler.select_profile(_requirements(), now=NOW)
    selected = scheduler.select_profile(
        _requirements(),
        now=NOW + timedelta(seconds=11),
    )

    assert selected.profile_id == "profile-temporary"
    assert registry.get_profile_state(
        "profile-temporary",
        now=NOW + timedelta(seconds=11),
    ).state == "ready"


def test_parallel_scheduling_isolates_profile_leases_and_sessions(tmp_path):
    from cli_agent_service.scheduler import AgentScheduler

    registry = _registry(tmp_path)
    for profile in (
        _profile("profile-a", account="account-a"),
        _profile("profile-b", account="account-b"),
    ):
        registry.register_profile(profile)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def schedule(run_id, owner_id):
        contender = _registry(tmp_path)
        barrier.wait()
        try:
            results.append(
                AgentScheduler(contender).schedule_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    role="dev",
                    project_id="aming-claw",
                    requirements=_requirements(),
                    ttl_seconds=120,
                    now=NOW,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=schedule, args=("run-a", "session-a")),
        threading.Thread(target=schedule, args=("run-b", "session-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert {item.selection.profile_id for item in results} == {
        "profile-a",
        "profile-b",
    }
    assert {item.lease.owner_id for item in results} == {"session-a", "session-b"}
    assert {
        item.run.profile.credential_ref.ref_id for item in results
    } == {
        "credential:codex-home:account-a",
        "credential:codex-home:account-b",
    }
    assert {
        item.run.profile.launcher_adapter.environment_keys for item in results
    } == {
        ("CODEX_HOME", "PROFILE_ACCOUNT-A"),
        ("CODEX_HOME", "PROFILE_ACCOUNT-B"),
    }

    state_barrier = threading.Barrier(2)
    state_errors = []

    def update_state(profile_id, signal, retry_at):
        contender = _registry(tmp_path)
        state_barrier.wait()
        try:
            contender.record_profile_signal(
                profile_id,
                signal,
                retry_at=retry_at,
                now=NOW,
            )
        except BaseException as exc:
            state_errors.append(exc)

    state_threads = [
        threading.Thread(
            target=update_state,
            args=("profile-a", "cooling_down", NOW + timedelta(seconds=30)),
        ),
        threading.Thread(
            target=update_state,
            args=("profile-b", "quota_exhausted", NOW + timedelta(seconds=60)),
        ),
    ]
    for thread in state_threads:
        thread.start()
    for thread in state_threads:
        thread.join(timeout=10)

    assert not state_errors
    assert not any(thread.is_alive() for thread in state_threads)
    states = {
        item.profile_id: item for item in _registry(tmp_path).list_profile_states(now=NOW)
    }
    assert states["profile-a"].state == "cooling_down"
    assert states["profile-a"].cooldown_until
    assert states["profile-a"].quota_reset_at == ""
    assert states["profile-b"].state == "quota_exhausted"
    assert states["profile-b"].cooldown_until == ""
    assert states["profile-b"].quota_reset_at


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        ("quota_exhausted", "quota_exhausted"),
        ("auth_required", "auth_required"),
        ("process_crash", "unhealthy"),
    ],
)
def test_failure_signal_changes_future_state_without_rotating_live_run(
    tmp_path,
    outcome,
    expected_state,
):
    from cli_agent_service.scheduler import AgentScheduler

    registry = _registry(tmp_path)
    for profile in (
        _profile("profile-a", account="account-a"),
        _profile("profile-b", account="account-b"),
    ):
        registry.register_profile(profile)
    scheduler = AgentScheduler(registry)
    live = scheduler.schedule_run(
        run_id="run-live",
        owner_id="live-session",
        role="dev",
        project_id="aming-claw",
        requirements=_requirements(profile_id="profile-a"),
        now=NOW,
    )

    state = scheduler.record_profile_outcome(
        "profile-a",
        outcome,
        run_id="run-live",
        retry_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    replacement = scheduler.schedule_run(
        run_id="run-future",
        owner_id="future-session",
        role="dev",
        project_id="aming-claw",
        requirements=_requirements(),
        now=NOW,
    )

    assert state.state == expected_state
    assert registry.get_run("run-live").lease.lease_id == live.lease.lease_id
    assert registry.get_run("run-live").run.config.profile_id == "profile-a"
    assert replacement.selection.profile_id == "profile-b"
    with pytest.raises(Exception, match="cannot be rotated"):
        scheduler.schedule_run(
            run_id="run-live",
            owner_id="live-session",
            role="dev",
            project_id="aming-claw",
            requirements=_requirements(profile_id="profile-b"),
            now=NOW,
        )


def test_existing_run_reacquires_expired_lease_without_profile_rotation(tmp_path):
    from cli_agent_service.scheduler import AgentScheduler

    registry = _registry(tmp_path)
    registry.register_profile(_profile("profile-a", account="account-a"))
    scheduler = AgentScheduler(registry)
    original = scheduler.schedule_run(
        run_id="run-stable",
        owner_id="stable-session",
        role="dev",
        project_id="aming-claw",
        requirements=_requirements(),
        ttl_seconds=5,
        now=NOW,
    )

    resumed = scheduler.schedule_run(
        run_id="run-stable",
        owner_id="stable-session",
        role="dev",
        project_id="aming-claw",
        requirements=_requirements(),
        ttl_seconds=30,
        now=NOW + timedelta(seconds=6),
    )

    assert resumed.run.config.profile_id == original.run.config.profile_id == "profile-a"
    assert resumed.lease.lease_id != original.lease.lease_id
    assert resumed.selection.selection_reason == "existing_run_identity_retained"
    assert resumed.selection.evidence_flags == ("live_run_profile_identity_immutable",)


def test_qa_prefers_distinct_profile_and_requires_explicit_fallback_evidence(tmp_path):
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    registry.register_profile(_profile("profile-a", account="qa-a"))
    registry.register_profile(_profile("profile-b", account="qa-b"))
    scheduler = AgentScheduler(registry)

    distinct = scheduler.select_qa_profile(
        "profile-a",
        _requirements(role="qa"),
        implementation_principal_id="worker-principal",
        qa_principal_id="qa-principal",
    )
    assert distinct.profile_id == "profile-b"
    assert distinct.qa_profile_distinct is True
    assert distinct.qa_principal_distinct is True
    assert distinct.same_profile_qa_fallback is False

    registry.set_profile_state("profile-b", "disabled", reason_code="disabled")
    with pytest.raises(NoEligibleProfileError):
        scheduler.select_qa_profile(
            "profile-a",
            _requirements(role="qa"),
            implementation_principal_id="worker-principal",
            qa_principal_id="qa-principal",
        )
    fallback = scheduler.select_qa_profile(
        "profile-a",
        _requirements(role="qa"),
        implementation_principal_id="worker-principal",
        qa_principal_id="qa-principal",
        allow_same_profile_fallback=True,
    )
    assert fallback.profile_id == "profile-a"
    assert fallback.same_profile_qa_fallback is True
    assert fallback.qa_principal_distinct is True
    assert fallback.evidence_flags == ("same_profile_qa_fallback_explicit",)
    assert fallback.to_public_dict()["governance_authority"] is False


@pytest.mark.parametrize(
    ("implementation_principal_id", "qa_principal_id"),
    (("", ""), ("worker-principal", ""), ("", "qa-principal")),
)
@pytest.mark.parametrize(
    "allow_same_profile_fallback",
    (False, True),
    ids=("distinct-profile", "same-profile-fallback"),
)
def test_qa_profile_selection_requires_both_principals(
    tmp_path,
    implementation_principal_id,
    qa_principal_id,
    allow_same_profile_fallback,
):
    from cli_agent_service.scheduler import AgentScheduler, SchedulerError

    registry = _registry(tmp_path)
    registry.register_profile(_profile("profile-a", account="qa-a"))
    if not allow_same_profile_fallback:
        registry.register_profile(_profile("profile-b", account="qa-b"))
    scheduler = AgentScheduler(registry)

    with pytest.raises(
        SchedulerError,
        match="implementation_principal_id and qa_principal_id are required",
    ):
        scheduler.select_qa_profile(
            "profile-a",
            _requirements(role="qa"),
            implementation_principal_id=implementation_principal_id,
            qa_principal_id=qa_principal_id,
            allow_same_profile_fallback=allow_same_profile_fallback,
        )


def test_scheduler_state_database_is_structured_private_and_non_authoritative(tmp_path):
    from cli_agent_service.registry import PersistenceRejectedError

    registry = _registry(tmp_path)
    registry.register_profile(_profile("profile-private", account="private"))
    registry.record_auth_required(
        "profile-private",
        reason_code="auth_expired",
        now=NOW,
    )
    with pytest.raises(PersistenceRejectedError):
        registry.set_profile_state(
            "profile-private",
            "unhealthy",
            reason_code="/private/provider/output",
        )

    with sqlite3.connect(registry.db_path) as conn:
        row = conn.execute(
            "SELECT state, reason_code, cooldown_until, quota_reset_at "
            "FROM agent_profile_states WHERE profile_id='profile-private'"
        ).fetchone()
        schema = "\n".join(
            item[0]
            for item in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        ).lower()
    assert row == ("auth_required", "auth_expired", "", "")
    assert "next_legal_action" not in schema
    assert "merge_authority" not in schema
    assert "close_authority" not in schema
    assert registry.get_profile_state("profile-private").to_public_dict()[
        "governance_authority"
    ] is False


def _local_requirements(**overrides):
    values = _requirements(
        provider="ollama",
        endpoint_id="endpoint-local",
        model="qwen3-coder",
        backend_mode="codex_oss",
        auth_mode="none",
    )
    values.update(overrides)
    return values


def _local_profile(profile_id="profile-local", **overrides):
    return _profile(
        profile_id,
        account="local",
        provider="ollama",
        endpoint_id="endpoint-local",
        model="qwen3-coder",
        backend_mode="codex_oss",
        auth_mode="none",
        **overrides,
    )


def _certification(profile, *, extra=(), omit=()):
    from cli_agent_service.certification import (
        CapabilityResult,
        CertificationScope,
        LocalModelCertification,
        ROLE_CAPABILITY_REQUIREMENTS,
    )

    required = {
        item.value for item in ROLE_CAPABILITY_REQUIREMENTS["worker"]
    }
    required.update(extra)
    required.difference_update(omit)
    return LocalModelCertification(
        CertificationScope.from_profile(profile),
        tuple(
            CapabilityResult(item, "passed", evidence_ref="probe:{}".format(item))
            for item in sorted(required)
        ),
    )


def test_local_endpoint_requires_capability_certification_not_health(tmp_path):
    from cli_agent_service.certification import (
        CapabilityResult,
        CertificationCatalog,
        CertificationScope,
        LocalModelCertification,
    )
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    profile = _local_profile()
    registry.register_profile(profile)

    with pytest.raises(NoEligibleProfileError) as missing:
        AgentScheduler(registry).select_profile(_local_requirements(), now=NOW)
    assert missing.value.evaluations[0].rejection_reasons == (
        "certification_missing",
        "role_not_certified",
    )

    discovery_only = LocalModelCertification(
        CertificationScope.from_profile(profile),
        (
            CapabilityResult("health", "passed"),
            CapabilityResult("model_discovery", "passed"),
        ),
    )
    with pytest.raises(NoEligibleProfileError) as uncertified:
        AgentScheduler(
            registry,
            certifications=CertificationCatalog((discovery_only,)),
        ).select_profile(_local_requirements(), now=NOW)
    assert "role_not_certified" in (
        uncertified.value.evaluations[0].rejection_reasons
    )


def test_scheduler_adds_certification_without_weakening_profile_policy(tmp_path):
    from cli_agent_service.certification import CertificationCatalog
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    profile = _local_profile()
    registry.register_profile(profile)
    catalog = CertificationCatalog((_certification(profile),))
    scheduler = AgentScheduler(registry, catalog)

    selected = scheduler.select_profile(_local_requirements(), now=NOW)
    assert selected.profile_id == profile.profile_id
    assert scheduler.certification_eligibility(profile, "dev", now=NOW).eligible

    with pytest.raises(NoEligibleProfileError) as provider_mismatch:
        scheduler.select_profile(
            _local_requirements(provider="openai_compatible"),
            now=NOW,
        )
    assert "provider_mismatch" in (
        provider_mismatch.value.evaluations[0].rejection_reasons
    )

    with pytest.raises(NoEligibleProfileError) as policy_denied:
        scheduler.select_profile(
            _local_requirements(role="observer"),
            now=NOW,
        )
    assert "role_not_allowed" in policy_denied.value.evaluations[0].rejection_reasons
    assert "role_not_certified" in policy_denied.value.evaluations[0].rejection_reasons


def test_scheduler_derives_qa_and_worker_eligibility_independently(tmp_path):
    from cli_agent_service.certification import CertificationCatalog
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    profile = _local_profile()
    registry.register_profile(profile)
    worker_only = _certification(profile)
    catalog = CertificationCatalog((worker_only,))
    scheduler = AgentScheduler(registry, catalog)

    assert scheduler.select_profile(_local_requirements(), now=NOW).profile_id == (
        profile.profile_id
    )
    with pytest.raises(NoEligibleProfileError) as qa_denied:
        scheduler.select_profile(
            _local_requirements(role="qa"),
            now=NOW,
        )
    assert "certification_read_only_tools_missing" in (
        qa_denied.value.evaluations[0].rejection_reasons
    )

    catalog.upsert(worker_only.with_capability("read_only_tools", "passed"))
    assert scheduler.select_profile(
        _local_requirements(role="qa"),
        now=NOW,
    ).profile_id == profile.profile_id


def test_scheduler_revokes_future_local_selection_after_degraded_outcome(tmp_path):
    from cli_agent_service.certification import CertificationCatalog
    from cli_agent_service.scheduler import AgentScheduler, NoEligibleProfileError

    registry = _registry(tmp_path)
    profile = _local_profile()
    registry.register_profile(profile)
    record = _certification(profile)
    catalog = CertificationCatalog((record,))
    scheduler = AgentScheduler(registry, certifications=catalog)
    assert scheduler.select_profile(_local_requirements(), now=NOW).profile_id == (
        profile.profile_id
    )

    catalog.upsert(
        record.with_capability(
            "reliability",
            "failed",
            reason_code="reliability_degraded",
            evidence_ref="probe:outcome-regression",
        )
    )
    with pytest.raises(NoEligibleProfileError) as denied:
        scheduler.select_profile(_local_requirements(), now=NOW)
    assert denied.value.evaluations[0].rejection_reasons == (
        "role_not_certified",
        "certification_reliability_failed",
    )
