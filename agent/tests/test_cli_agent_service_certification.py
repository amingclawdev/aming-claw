import json
import sys
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _scope(*, model="local-model", policy_version="1"):
    from cli_agent_service.certification import CertificationScope

    return CertificationScope(
        runtime_id="runtime-codex-oss",
        runtime_version="1",
        endpoint_id="endpoint-local",
        endpoint_version="1",
        model=model,
        policy_id="policy-local",
        policy_version=policy_version,
        provider="ollama",
    )


def _passed(*capabilities):
    from cli_agent_service.certification import CapabilityResult

    return tuple(
        CapabilityResult(capability, "passed", evidence_ref="probe:{}".format(capability))
        for capability in capabilities
    )


def _all_worker_capabilities():
    from cli_agent_service.certification import ROLE_CAPABILITY_REQUIREMENTS

    return tuple(item.value for item in ROLE_CAPABILITY_REQUIREMENTS["worker"])


def test_health_and_model_discovery_never_grant_a_role():
    from cli_agent_service.certification import LocalModelCertification

    record = LocalModelCertification(
        _scope(),
        _passed("health", "model_discovery"),
    )

    for role in ("utility", "worker", "observer", "qa"):
        assessment = record.role_eligibility(role)
        assert assessment.eligible is False
        assert assessment.missing_capabilities
    assert record.eligible_roles() == ()


def test_role_capabilities_are_independent_and_explainable():
    from cli_agent_service.certification import LocalModelCertification

    worker = LocalModelCertification(
        _scope(),
        _passed(*_all_worker_capabilities()),
    )

    assert worker.role_eligibility("utility").eligible is True
    assert worker.role_eligibility("dev").eligible is True
    assert worker.role_eligibility("dev").bounded is True
    observer = worker.role_eligibility("observer")
    qa = worker.role_eligibility("qa")
    assert observer.eligible is False
    assert observer.missing_capabilities == ("read_only_tools",)
    assert qa.eligible is False
    assert qa.missing_capabilities == ("read_only_tools",)

    stronger = worker.with_capability("read_only_tools", "passed")
    assert stronger.role_eligibility("observer").eligible is True
    assert stronger.role_eligibility("qa").eligible is True


def test_failure_or_revocation_removes_only_roles_that_need_the_capability():
    from cli_agent_service.certification import LocalModelCertification

    record = LocalModelCertification(
        _scope(),
        _passed(*_all_worker_capabilities(), "read_only_tools"),
    )
    degraded = record.with_capability(
        "isolated_worktree_edit",
        "revoked",
        reason_code="worktree_probe_regressed",
        evidence_ref="probe:regression-1",
    )

    worker = degraded.role_eligibility("worker")
    assert worker.eligible is False
    assert worker.revoked_capabilities == ("isolated_worktree_edit",)
    assert degraded.role_eligibility("observer").eligible is True
    assert degraded.role_eligibility("qa").eligible is True


def test_reliability_outcomes_can_revoke_eligibility_after_degradation():
    from cli_agent_service.certification import LocalModelCertification

    record = LocalModelCertification(
        _scope(),
        _passed(*_all_worker_capabilities()),
        reliability_successes=2,
        reliability_samples=2,
    )
    assert record.role_eligibility("worker").eligible is True

    degraded = record.record_reliability_outcome(
        False,
        minimum_samples=3,
        minimum_success_rate=0.8,
        evidence_ref="probe:outcome-3",
    )

    assert degraded.result_for("reliability").status.value == "failed"
    assert degraded.role_eligibility("worker").eligible is False
    assert degraded.role_eligibility("worker").failed_capabilities == ("reliability",)


def test_catalog_requires_an_exact_runtime_endpoint_model_and_policy_scope():
    from cli_agent_service.certification import CertificationCatalog, LocalModelCertification

    record = LocalModelCertification(_scope(), _passed("structured_output"))
    catalog = CertificationCatalog((record,))

    assert catalog.get(_scope()) is record
    assert catalog.get(_scope(model="other-model")) is None
    assert catalog.get(_scope(policy_version="2")) is None


def test_certification_projection_is_structured_and_public_safe():
    from cli_agent_service.certification import LocalModelCertification

    record = LocalModelCertification(
        _scope(),
        _passed("structured_output", "context_limits", "reliability"),
    )
    rendered = json.dumps(record.to_public_dict(), sort_keys=True)

    assert "raw_probe_output_exposed" in rendered
    assert '"raw_credentials_exposed": false' in rendered
    assert "api_key" not in rendered
    assert "session_token" not in rendered
    assert "route_token" not in rendered
    assert record.to_public_dict()["role_eligibility"]["utility"]["eligible"] is True
