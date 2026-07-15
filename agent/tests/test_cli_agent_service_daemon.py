import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for {}".format(path))


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(AGENT_DIR)
    return env


class _RecordingSupervisor:
    def __init__(self, registry):
        from cli_agent_service.launchers import HostEnvelopeStore

        self.registry = registry
        self.host_envelope_store = HostEnvelopeStore()
        self.owner_id = "daemon-guided-owner"
        self.run_receipt_sink = None
        self.managed_profile_home_resolver = None
        self.lease_ttl_seconds = 60
        self.starts = []
        self.start_leases = []
        self.host_envelope_consumed = []
        self.consumed_environment_keys = []

    def start_run(self, run, **kwargs):
        record = self.registry.get_run(run.run_id)
        assert record is not None
        assert record.lease is not None
        assert record.lease.status == "active"
        assert record.lease.owner_id == self.owner_id
        self.start_leases.append(record.lease)
        delivery = self.host_envelope_store.consume(
            run.run_id,
            lease_owner_id=self.owner_id,
            lease_id=record.lease.lease_id,
        )
        self.host_envelope_consumed.append(delivery is not None)
        environment = {}
        if delivery is not None:
            delivery.apply_to(environment)
            delivery.discard()
        self.consumed_environment_keys.append(tuple(sorted(environment)))
        environment.clear()
        self.starts.append((run, dict(kwargs)))
        return object()


def _guided_profile(
    *,
    profile_id="profile-codex-a",
    backend_mode="codex_cli",
    max_concurrency=1,
    roles=("mf_sub",),
    ref_kind="inherited_current",
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
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex-guided",
            kind="codex_cli",
            executable_ref="path:/usr/bin/true",
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-codex-guided",
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode=backend_mode,
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:inherited",
            provider="openai",
            ref_kind=ref_kind,
        ),
        launcher_adapter=LauncherAdapter(launcher_id="launcher-codex-guided"),
        role_policy=RolePolicy(
            policy_id="policy-codex-guided",
            roles=roles,
            project_ids=("aming-claw",),
            max_concurrency=max_concurrency,
        ),
    )


def _guided_host_envelope(
    session_token="worker-session-guided",
    fence_token="worker-fence-guided",
):
    return {
        "env": {
            "AMING_WORKER_SESSION_TOKEN": session_token,
            "AMING_WORKER_FENCE_TOKEN": fence_token,
        }
    }


def _guided_ticket_and_selectors(
    tmp_path,
    *,
    profile_id="profile-codex-a",
    profile_role="mf_sub",
    retry_policy=None,
    role="mf_sub",
):
    from governance.contract_state_runtime import build_cli_agent_execution_ticket
    from cli_agent_service.service import _stable_json_hash

    route_identity = {
        "route_id": "route-guided-daemon",
        "route_context_hash": "sha256:" + ("b" * 64),
        "prompt_contract_id": "rprompt-guided-daemon",
        "prompt_contract_hash": "sha256:" + ("c" * 64),
        "route_token_ref": "rtok-guided-daemon",
        "visible_injection_manifest_hash": "sha256:" + ("d" * 64),
    }
    is_qa = role == "qa"
    task_id = "qa-task-guided-daemon" if is_qa else "task-guided-daemon"
    worker_id = "qa:task-guided-daemon" if is_qa else "worker-guided-daemon"
    profile_requirements = {
        "role": role,
        "harness": "codex",
        "provider": "openai",
        "model": "gpt-5.4-codex",
    }
    if not is_qa:
        profile_requirements["profile_id"] = profile_id
    action = {
        "id": "qa_graph_context" if is_qa else "worker_dispatch",
        "action": "dispatch_bounded_qa" if is_qa else "dispatch_bounded_worker",
        "stage_id": "qa" if is_qa else "dispatch",
        "line_id": (
            "qa_graph_context" if is_qa else "observer_dispatch_bounded_workers"
        ),
        "evidence_kind": "graph_trace" if is_qa else "dispatch_bounded_worker",
        "owner_role": "qa" if is_qa else "observer",
        "worker_role": role,
        "runtime_context_id": "mfrctx-guided-daemon",
        "task_id": task_id,
        "worker_id": worker_id,
        "worker_slot_id": worker_id,
        "observer_command_id": "command-guided-daemon",
        "parent_task_id": "AC-GUIDED-DAEMON",
        "target_project_root": str(tmp_path),
        "branch_ref": "refs/heads/codex/guided-daemon",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "merge_queue_id": "mq-guided-daemon",
        "owned_files": ["agent/owned.py"],
        **route_identity,
        "profile_requirements": profile_requirements,
        "retry_policy": dict(
            retry_policy or {"attempt": 1, "max_attempts": 1}
        ),
    }
    launch_identity = {
        "project_id": "aming-claw",
        "backlog_id": "AC-GUIDED-DAEMON",
        "task_id": action["task_id"],
        "worker_id": action["worker_id"],
        "worker_slot_id": action["worker_slot_id"],
        "observer_command_id": action["observer_command_id"],
        "parent_task_id": action["parent_task_id"],
        "runtime_context_id": action["runtime_context_id"],
        "worker_role": action["worker_role"],
        "target_project_root": action["target_project_root"],
        "worktree_path": action["target_project_root"],
        "branch_ref": action["branch_ref"],
        "base_commit": action["base_commit"],
        "target_head_commit": action["target_head_commit"],
        "merge_queue_id": action["merge_queue_id"],
        "owned_files": action["owned_files"],
        **route_identity,
    }
    authority = {
        "source_of_authority": "ContractRuntime",
        "authority_decision_source": (
            "contract_runtime_qa_execution_ticket"
            if is_qa
            else "contract_runtime_completed_dispatch_line"
        ),
        "project_id": "aming-claw",
        "backlog_id": "AC-GUIDED-DAEMON",
        "contract_execution_id": "cex-guided-daemon",
        "contract_revision_id": "revision-guided-daemon",
        "execution_state_revision": 7,
        "execution_state_hash": "sha256:" + ("e" * 64),
        "runtime_guide_hash": "sha256:" + ("f" * 64),
        "readiness_state": "contract_active",
        "next_legal_action": action,
    }
    ticket = build_cli_agent_execution_ticket(
        contract_runtime_current_state=authority,
        launch_identity=launch_identity,
        expected_execution_state_revision=7,
    )
    assert ticket["status"] == "issued", ticket
    if profile_role is None:
        ticket["profile_requirements"].pop("role", None)
    else:
        ticket["profile_requirements"]["role"] = profile_role
    ticket["profile_requirements_hash"] = _stable_json_hash(
        ticket["profile_requirements"]
    )
    ticket_material = dict(ticket)
    ticket_material.pop("ticket_hash", None)
    ticket["ticket_hash"] = _stable_json_hash(ticket_material)
    selectors = {
        "project_id": "aming-claw",
        "backlog_id": "AC-GUIDED-DAEMON",
        "contract_execution_id": authority["contract_execution_id"],
        "runtime_context_id": action["runtime_context_id"],
        "task_id": action["task_id"],
        "worker_id": action["worker_id"],
        "worker_slot_id": action["worker_slot_id"],
        "observer_command_id": action["observer_command_id"],
        "role": action["worker_role"],
        "principal_id": action["worker_id"],
        "expected_execution_state_revision": 7,
        "expected_execution_state_hash": authority["execution_state_hash"],
        "expected_dispatch_identity_hash": ticket["dispatch_identity_hash"],
        **route_identity,
        "harness": "codex",
        "provider": "openai",
        "model": "gpt-5.4-codex",
        "backend_mode": "codex_cli",
    }
    if not is_qa:
        selectors["profile_id"] = profile_id
    return ticket, selectors


def test_daemon_start_status_health_and_stop(tmp_path):
    from cli_agent_service.service import ServicePaths

    state_dir = tmp_path / "private-state"
    paths = ServicePaths.from_state_dir(state_dir)
    command = [
        sys.executable,
        "-m",
        "cli_agent_service",
        "start",
        "--state-dir",
        str(state_dir),
    ]
    process = subprocess.Popen(command, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(paths.socket_path)
        status = subprocess.run(
            [sys.executable, "-m", "cli_agent_service", "status", "--state-dir", str(state_dir)],
            env=_env(), check=False, capture_output=True, text=True, timeout=5,
        )
        payload = json.loads(status.stdout)
        assert status.returncode == 0
        assert payload["status"] == "running"
        assert payload["accepting_agent_runs"] is False
        assert payload["raw_credentials_exposed"] is False

        health = subprocess.run(
            [sys.executable, "-m", "cli_agent_service", "health", "--state-dir", str(state_dir)],
            env=_env(), check=False, capture_output=True, text=True, timeout=5,
        )
        assert json.loads(health.stdout)["ok"] is True
        assert os.stat(state_dir).st_mode & 0o777 == 0o700
        assert os.stat(paths.socket_path).st_mode & 0o777 == 0o600
        assert os.stat(state_dir / "status.json").st_mode & 0o777 == 0o600

        stopped = subprocess.run(
            [sys.executable, "-m", "cli_agent_service", "stop", "--state-dir", str(state_dir)],
            env=_env(), check=False, capture_output=True, text=True, timeout=5,
        )
        assert stopped.returncode == 0
        assert json.loads(stopped.stdout)["status"] == "stopping"
        assert process.wait(timeout=5) == 0
        assert json.loads((state_dir / "status.json").read_text())["status"] == "stopped"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_health_projection_is_deterministic_and_public_safe():
    from cli_agent_service.health import health_payload

    started = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    current = datetime(2026, 7, 12, 12, 0, 7, tzinfo=timezone.utc)
    payload = health_payload(pid=42, started_at=started, socket_ready=True, now=current)
    assert payload == {
        "schema_version": "cli_agent_service.health.v1",
        "service": "cli_agent_service",
        "ok": True,
        "status": "running",
        "pid": 42,
        "started_at": "2026-07-12T12:00:00.000000Z",
        "uptime_seconds": 7,
        "socket_ready": True,
        "accepting_agent_runs": False,
        "raw_credentials_exposed": False,
    }


def test_daemon_socket_rejects_caller_owned_desktop_authority(tmp_path):
    from cli_agent_service.service import ServicePaths, request_service

    state_dir = tmp_path / "private-state"
    paths = ServicePaths.from_state_dir(state_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_service",
            "start",
            "--state-dir",
            str(state_dir),
        ],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(paths.socket_path)
        response = request_service(
            paths,
            "desktop_execution_ticket_admit",
            payload={
                "host_kind": "codex_desktop",
                "project_id": "aming-claw",
                "backlog_id": "AC-FORGED",
                "contract_execution_id": "cex-forged",
                "runtime_context_id": "mfrctx-forged",
                "task_id": "task-forged",
                "worker_id": "worker-forged",
                "worker_slot_id": "slot-forged",
                "observer_command_id": "command-forged",
                "contract_runtime_current_state": {"source_of_authority": "ContractRuntime"},
                "execution_ticket": {"status": "issued", "issue_allowed": True},
            },
        )
        assert response["ok"] is False
        assert response["status"] == "invalid_request"
        assert "unsupported authority fields" in response["error"]
    finally:
        if process.poll() is None:
            try:
                request_service(paths, "stop")
            except Exception:
                process.terminate()
            process.wait(timeout=5)


def test_daemon_socket_admits_when_optional_authority_hashes_are_omitted(tmp_path):
    from agent.tests.test_cli_agent_service_desktop import (
        _admission_payload,
        _ticket,
    )
    from cli_agent_service.service import (
        CliAgentService,
        ServicePaths,
        request_service,
    )

    paths = ServicePaths.from_state_dir(tmp_path / "private-state")
    ticket = _ticket()
    resolver_requests = []

    def resolver(request):
        resolver_requests.append(dict(request))
        return dict(ticket)

    service = CliAgentService(paths)
    service._contract_runtime_authority_resolver = resolver
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_for(paths.socket_path)
        payload = _admission_payload()
        payload.pop("expected_execution_state_hash")
        payload.pop("expected_dispatch_identity_hash")

        response = request_service(
            paths,
            "desktop_execution_ticket_admit",
            payload=payload,
        )

        assert response["ok"] is True
        assert response["status"] == "admitted"
        assert response["execution_ticket"] == ticket
        assert resolver_requests == [
            {
                key: value
                for key, value in payload.items()
                if key not in {"host_kind", "now_iso"}
            }
        ]
    finally:
        if thread.is_alive():
            request_service(paths, "stop")
        thread.join(timeout=5)
        assert thread.is_alive() is False


def test_daemon_owns_ticket_profile_run_and_single_supervisor_start(tmp_path):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(tmp_path)
    resolver_requests = []

    def resolver(request):
        resolver_requests.append(dict(request))
        return dict(ticket)

    service._contract_runtime_authority_resolver = resolver
    schedule_calls = []
    schedule_run = service.scheduler.schedule_run

    def recording_schedule_run(**kwargs):
        scheduled = schedule_run(**kwargs)
        record = registry.get_run(scheduled.run.run_id)
        assert record is not None
        assert record.run.to_public_dict() == scheduled.run.to_public_dict()
        assert record.lease == scheduled.lease
        schedule_calls.append((dict(kwargs), scheduled))
        return scheduled

    service.scheduler.schedule_run = recording_schedule_run
    session_token = "worker-session-guided-success"
    fence_token = "worker-fence-guided-success"
    response, should_stop = service._dispatch(
        {
            "operation": "start_host_envelope_run",
            "payload": {
                "authority_selectors": selectors,
                "host_envelope": _guided_host_envelope(
                    session_token,
                    fence_token,
                ),
            },
        }
    )

    assert should_stop is False
    assert response["ok"] is True
    assert response["status"] == "started"
    assert response["profile_id"] == "profile-codex-a"
    assert response["run_id"] == "run-{}".format(ticket["ticket_id"])
    assert response["direct_invocation_fallback"] is False
    assert len(resolver_requests) == 1
    assert not {
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
        "backend_mode",
        "provider",
        "model",
        "harness",
    }.intersection(resolver_requests[0])
    assert len(schedule_calls) == 1
    schedule_request, scheduled = schedule_calls[0]
    assert schedule_request["run_id"] == response["run_id"]
    assert schedule_request["owner_id"] == supervisor.owner_id
    assert schedule_request["profile_requirements"]["profile_id"] == (
        "profile-codex-a"
    )
    assert schedule_request["profile_requirements"]["backend_mode"] == (
        "codex_cli"
    )
    assert scheduled.lease.status == "active"
    assert len(supervisor.starts) == 1
    run, start = supervisor.starts[0]
    assert run.run_id == response["run_id"]
    assert run.profile == registry.get_profile("profile-codex-a")
    assert run.config.profile_id == "profile-codex-a"
    assert run.config.provider == "openai"
    assert run.config.model == "gpt-5.4-codex"
    assert run.config.backend_mode == "codex_cli"
    assert start["worktree"] == str(tmp_path)
    assert start["execution_ticket"] == ticket
    assert start["require_host_envelope"] is True
    assert "ContractRuntime" in start["prompt"]
    assert "After each accepted ContractRuntime line" in start["prompt"]
    assert "worker startup, graph context" in start["prompt"]
    assert "worker commit, finish-time worker attestation" in start["prompt"]
    assert "and finish gate" in start["prompt"]
    assert "Stop only at terminal completion or a real blocker" in start["prompt"]
    assert "remain within the allocated worker scope" in start["prompt"]
    assert "$aming-claw:aming-claw-onboard" not in start["prompt"]
    assert "work_type=qa_verification" not in start["prompt"]
    assert supervisor.start_leases == [scheduled.lease]
    assert supervisor.host_envelope_consumed == [True]
    assert supervisor.consumed_environment_keys == [
        ("AMING_WORKER_FENCE_TOKEN", "AMING_WORKER_SESSION_TOKEN")
    ]
    registered = registry.get_run(response["run_id"])
    assert registered is not None
    assert registered.run.to_public_dict() == run.to_public_dict()
    assert registered.lease == scheduled.lease
    with sqlite3.connect(registry.db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_leases").fetchone()[0]
            == 1
        )

    public = json.dumps(response, sort_keys=True)
    for forbidden in (
        '"prompt":',
        '"argv":',
        '"environment":',
        "CODEX_HOME",
        '"host_envelope":',
        '"execution_ticket":',
    ):
        assert forbidden not in public
    assert response["caller_run_accepted"] is False
    assert response["caller_prompt_accepted"] is False
    assert response["caller_environment_accepted"] is False
    assert response["transient_host_envelope_required"] is True
    assert response["transient_host_envelope_accepted"] is True
    assert response["transient_host_envelope_consumed"] is True
    assert response["transient_host_envelope_persisted"] is False
    assert response["host_envelope_run_authority"] is False
    assert response["raw_provider_output_persisted"] is False
    assert response["provider_output_suppressed"] is True
    assert session_token not in public
    assert fence_token not in public


def test_daemon_qa_prompt_bootstraps_authoritative_bounded_verification(
    tmp_path,
    monkeypatch,
):
    import cli_agent_service.service as service_module
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import (
        CliAgentService,
        ServiceError,
        ServicePaths,
        _stable_json_hash,
    )
    from governance import contract_state_runtime

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile(roles=("qa",)))
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    old_ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role="qa",
        role="qa",
    )
    assert "profile_id" not in old_ticket["profile_requirements"]
    assert "profile_id" not in selectors
    assert "qa_bootstrap_guide_contract" not in selectors
    assert "qa_onboard_guidance_contract" not in selectors
    assert "managed_profile_tooling_contract" not in selectors
    raw_qa_token = "raw-qa-session-token-must-not-appear"
    active_ticket = {"value": old_ticket}
    schedule_requests = []
    schedule_run = service.scheduler.schedule_run

    def recording_schedule_run(**kwargs):
        schedule_requests.append(dict(kwargs))
        return schedule_run(**kwargs)

    service.scheduler.schedule_run = recording_schedule_run

    def resolve_ticket(request, *, qa_session_token):
        assert request["task_id"] == "qa-task-guided-daemon"
        assert "qa_bootstrap_guide_contract" not in request
        assert "qa_onboard_guidance_contract" not in request
        assert "managed_profile_tooling_contract" not in request
        assert qa_session_token == raw_qa_token
        return dict(active_ticket["value"])

    monkeypatch.setattr(
        service_module,
        "resolve_governance_execution_ticket",
        resolve_ticket,
    )

    for caller_field, caller_value in (
        ("prompt", "caller prompt must not replace the v5 guide"),
        ("environment", {"CALLER_ENV_MUST_NOT_APPEAR": "secret"}),
    ):
        with pytest.raises(ServiceError, match="unsupported fields"):
            service._admit_governed_host_envelope_run(
                {
                    "authority_selectors": selectors,
                    "qa_session_token": raw_qa_token,
                    caller_field: caller_value,
                },
                qa_mode=True,
            )
    assert schedule_requests == []
    assert supervisor.starts == []

    for invalid_binding in (None, {"tooling_hash": "sha256:stale"}):
        invalid_ticket = dict(old_ticket)
        if invalid_binding is None:
            invalid_ticket.pop("managed_profile_tooling_contract")
        else:
            invalid_ticket["managed_profile_tooling_contract"] = invalid_binding
        invalid_material = dict(invalid_ticket)
        invalid_material.pop("ticket_hash", None)
        invalid_ticket["ticket_hash"] = _stable_json_hash(invalid_material)
        active_ticket["value"] = invalid_ticket
        with pytest.raises(ServiceError, match="managed_profile_tooling_contract"):
            service._admit_governed_host_envelope_run(
                {
                    "authority_selectors": selectors,
                    "qa_session_token": raw_qa_token,
                },
                qa_mode=True,
            )
    assert schedule_requests == []
    assert supervisor.starts == []

    for invalid_binding in (None, {"guidance_hash": "sha256:stale"}):
        invalid_ticket = dict(old_ticket)
        if invalid_binding is None:
            invalid_ticket.pop("qa_onboard_guidance_contract")
        else:
            invalid_ticket["qa_onboard_guidance_contract"] = invalid_binding
        invalid_material = dict(invalid_ticket)
        invalid_material.pop("ticket_hash", None)
        invalid_ticket["ticket_hash"] = _stable_json_hash(invalid_material)
        active_ticket["value"] = invalid_ticket
        with pytest.raises(ServiceError, match="qa_onboard_guidance_contract"):
            service._admit_governed_host_envelope_run(
                {
                    "authority_selectors": selectors,
                    "qa_session_token": raw_qa_token,
                },
                qa_mode=True,
            )
    assert schedule_requests == []
    assert supervisor.starts == []
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    active_ticket["value"] = old_ticket

    old_response = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "qa_session_token": raw_qa_token,
        },
        qa_mode=True,
    )
    registry.record_exit(old_response["run_id"], 0)
    old_record = registry.get_run(old_response["run_id"])
    assert old_record is not None
    assert old_record.state == "completed"

    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_MANAGED_PROFILE_TOOLING_VERSION",
        contract_state_runtime.CLI_AGENT_MANAGED_PROFILE_TOOLING_VERSION
        + ".daemon-change",
    )
    with pytest.raises(ServiceError, match="managed_profile_tooling_contract"):
        service._admit_governed_host_envelope_run(
            {
                "authority_selectors": selectors,
                "qa_session_token": raw_qa_token,
            },
            qa_mode=True,
        )
    assert len(schedule_requests) == 1
    tooling_ticket, tooling_selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role="qa",
        role="qa",
    )
    assert tooling_selectors == selectors
    assert tooling_ticket["ticket_id"] != old_ticket["ticket_id"]
    assert tooling_ticket["dispatch_identity_hash"] == old_ticket[
        "dispatch_identity_hash"
    ]
    assert tooling_ticket["retry_policy"] == old_ticket["retry_policy"]
    assert tooling_ticket["managed_profile_tooling_contract"] != old_ticket[
        "managed_profile_tooling_contract"
    ]
    active_ticket["value"] = tooling_ticket
    tooling_response = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "qa_session_token": raw_qa_token,
        },
        qa_mode=True,
    )
    registry.record_exit(tooling_response["run_id"], 0)
    assert registry.get_run(old_response["run_id"]).state == "completed"
    assert registry.get_run(tooling_response["run_id"]).state == "completed"

    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_ONBOARD_GUIDANCE_VERSION",
        contract_state_runtime.CLI_AGENT_QA_ONBOARD_GUIDANCE_VERSION
        + ".daemon-change",
    )
    with pytest.raises(ServiceError, match="qa_onboard_guidance_contract"):
        service._admit_governed_host_envelope_run(
            {
                "authority_selectors": selectors,
                "qa_session_token": raw_qa_token,
            },
            qa_mode=True,
        )
    assert len(schedule_requests) == 2
    onboard_ticket, onboard_selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role="qa",
        role="qa",
    )
    assert onboard_selectors == selectors
    assert onboard_ticket["ticket_id"] != tooling_ticket["ticket_id"]
    assert onboard_ticket["dispatch_identity_hash"] == old_ticket[
        "dispatch_identity_hash"
    ]
    assert onboard_ticket["profile_requirements"] == tooling_ticket[
        "profile_requirements"
    ]
    assert onboard_ticket["retry_policy"] == old_ticket["retry_policy"]
    assert onboard_ticket["qa_bootstrap_guide_contract"] == old_ticket[
        "qa_bootstrap_guide_contract"
    ]
    assert onboard_ticket["managed_profile_tooling_contract"] == tooling_ticket[
        "managed_profile_tooling_contract"
    ]
    active_ticket["value"] = onboard_ticket
    onboard_response = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "qa_session_token": raw_qa_token,
        },
        qa_mode=True,
    )
    registry.record_exit(onboard_response["run_id"], 0)
    assert registry.get_run(onboard_response["run_id"]).state == "completed"

    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_BOOTSTRAP_GUIDE_PROMPT_TEMPLATE",
        contract_state_runtime.CLI_AGENT_QA_BOOTSTRAP_GUIDE_PROMPT_TEMPLATE
        + "\nVersioned daemon guide change.",
    )
    with pytest.raises(ServiceError, match="qa_bootstrap_guide_contract"):
        service._admit_governed_host_envelope_run(
            {
                "authority_selectors": selectors,
                "qa_session_token": raw_qa_token,
            },
            qa_mode=True,
        )
    assert len(schedule_requests) == 3
    new_ticket, new_selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role="qa",
        role="qa",
    )
    assert new_selectors == selectors
    assert new_ticket["ticket_id"] != tooling_ticket["ticket_id"]
    assert new_ticket["dispatch_identity_hash"] == old_ticket[
        "dispatch_identity_hash"
    ]
    assert new_ticket["retry_policy"] == old_ticket["retry_policy"]
    assert new_ticket["ticket_id"] != onboard_ticket["ticket_id"]
    active_ticket["value"] = new_ticket

    response = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "qa_session_token": raw_qa_token,
        },
        qa_mode=True,
    )

    assert response["status"] == "started"
    assert response["role"] == "qa"
    assert response["run_id"] != old_response["run_id"]
    assert registry.get_run(old_response["run_id"]).state == "completed"
    assert registry.get_run(tooling_response["run_id"]).state == "completed"
    assert registry.get_run(onboard_response["run_id"]).state == "completed"
    assert registry.get_run(response["run_id"]) is not None
    assert len(supervisor.starts) == 4
    assert len(schedule_requests) == 4
    for schedule_request in schedule_requests:
        assert "qa_bootstrap_guide_contract" not in schedule_request[
            "profile_requirements"
        ]
        assert "qa_bootstrap_guide_contract" not in schedule_request
        assert "qa_onboard_guidance_contract" not in schedule_request[
            "profile_requirements"
        ]
        assert "qa_onboard_guidance_contract" not in schedule_request
        assert "managed_profile_tooling_contract" not in schedule_request[
            "profile_requirements"
        ]
        assert "managed_profile_tooling_contract" not in schedule_request
    _run, start = supervisor.starts[-1]
    prompt = start["prompt"]
    for coordinate in (
        "project_id=aming-claw",
        "backlog_id=AC-GUIDED-DAEMON",
        "contract_execution_id=cex-guided-daemon",
        "runtime_context_id=mfrctx-guided-daemon",
        "qa_task_id=qa-task-guided-daemon",
        "original_worker_task_id=task-guided-daemon",
        "principal_id=qa:task-guided-daemon",
        "assigned_worktree={}".format(tmp_path),
    ):
        assert coordinate in prompt
    skill_token = "$aming-claw:aming-claw-onboard"
    onboard_instruction = (
        "Immediately use that skill to call managed MCP "
        "`onboard_route_guide` with exactly project_id=aming-claw, "
        "backlog_id=AC-GUIDED-DAEMON, role=qa, and "
        "work_type=qa_verification."
    )
    assert prompt.startswith(skill_token + "\n")
    assert prompt.count(skill_token) == 1
    assert onboard_instruction in prompt
    assert "Do not guess or call a curl endpoint" in prompt
    assert "`git rev-parse HEAD`" in prompt
    assert "full candidate SHA" in prompt
    assert "do not trust the pre-dispatch target_head_commit" in prompt
    assert "qa_session_token or X-Gov-Token" in prompt
    assert "never echo it, write it to a file, or write it to timeline" in prompt
    assert "compact read-only CLI projections" in prompt
    assert "ContractRuntime remains the source of authority" in prompt
    for graph_argument in (
        "tool=query_schema",
        "query_source=qa",
        "query_purpose=independent_verification",
        "project_id=aming-claw",
        "backlog_id=AC-GUIDED-DAEMON",
        "task_id=task-guided-daemon",
        "commit_sha=<full git HEAD>",
        "repo_root={}".format(tmp_path),
        "qa_session_token=<raw token from step 4>",
    ):
        assert graph_argument in prompt
    assert "writer_role_safe_copy_payload.copy_payload unchanged" in prompt
    assert "schema_version=mf_parallel.qa_graph_context.v1" in prompt
    assert "graph_trace_ids=[<returned trace_id>]" in prompt
    assert "graph_query_trace_ids=[<returned trace_id>]" in prompt
    assert "tests list records every exact pytest node id and outcome" in prompt
    assert "starts with a clear PASS: or FAIL:" in prompt
    assert "execution_state_revision to be strictly greater" in prompt
    assert "Before this graph call succeeds, do not read ContractRuntime" in prompt
    assert "run tests, or send a final response" in prompt
    assert "If graph_query returns an error" in prompt
    assert "report only a public blocker" in prompt
    assert "Read-only current/guide calls and a process exit 0 are not completion" in (
        prompt
    )
    ordered_steps = (
        skill_token,
        onboard_instruction,
        "3. In assigned_worktree, run exactly `git rev-parse HEAD`",
        "4. Call qa_session_register",
        "5. Immediately call managed MCP `graph_query`",
        "Only after graph_query returns a successful trace_id",
        "managed MCP `contract_runtime_current`",
        "managed MCP `contract_runtime_guide`",
        "Call managed MCP `contract_runtime_submit_line` for qa_graph_context",
        "After qa_graph_context is accepted, re-read both managed MCP",
        "run only the refreshed guide's focused exact pytest node ids",
        "exactly once for qa_independent_verification",
        "Re-read both managed MCP projections and require",
    )
    assert [prompt.index(step) for step in ordered_steps] == sorted(
        prompt.index(step) for step in ordered_steps
    )
    onboard_index = prompt.index(onboard_instruction)
    for forbidden_before_onboard in (
        "4. Call qa_session_register",
        "5. Immediately call managed MCP `graph_query`",
        "managed MCP `contract_runtime_current`",
        "run only the refreshed guide's focused exact pytest node ids",
        "or send a final response",
    ):
        assert onboard_index < prompt.index(forbidden_before_onboard)
    assert "If tests pass but runtime did not advance, report an explicit blocker" in prompt
    assert "do not declare operational success" in prompt
    assert "use only rg, head, narrow sed ranges, and exact pytest node ids" in prompt
    assert "Never dump full runtime, timeline, large files, or raw provider output" in prompt
    assert raw_qa_token not in prompt
    assert start["worktree"] == str(tmp_path)
    assert start["require_host_envelope"] is False


def test_daemon_reports_managed_tooling_failure_before_spawn(tmp_path, monkeypatch):
    import cli_agent_service.service as service_module
    from cli_agent_service.profile_control import ProfileToolingError
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(
        _guided_profile(roles=("qa",), ref_kind="provider_home")
    )
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role="qa",
        role="qa",
    )

    def resolve_ticket(_request, *, qa_session_token):
        assert qa_session_token == "transient-qa-token"
        return dict(ticket)

    def fail_profile_tooling(_profile):
        raise ProfileToolingError("injected tooling failure")

    monkeypatch.setattr(
        service_module,
        "resolve_governance_execution_ticket",
        resolve_ticket,
    )
    service.profile_control.resolve_profile_home = fail_profile_tooling

    with pytest.raises(
        ServiceError,
        match="managed Codex profile tooling bootstrap failed",
    ):
        service._admit_governed_host_envelope_run(
            {
                "authority_selectors": selectors,
                "qa_session_token": "transient-qa-token",
            },
            qa_mode=True,
        )

    record = registry.get_run("run-{}".format(ticket["ticket_id"]))
    assert record is not None
    assert record.state == "failed"
    assert record.lease is None
    assert supervisor.starts == []


def test_daemon_admits_absent_profile_role_and_passes_canonical_role_to_scheduler(
    tmp_path,
):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role=None,
    )
    assert "role" not in ticket["profile_requirements"]
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)
    schedule_requests = []
    schedule_run = service.scheduler.schedule_run

    def recording_schedule_run(**kwargs):
        schedule_requests.append(dict(kwargs))
        return schedule_run(**kwargs)

    service.scheduler.schedule_run = recording_schedule_run

    response = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "host_envelope": _guided_host_envelope(),
        }
    )

    assert response["status"] == "started"
    assert response["role"] == "mf_sub"
    assert len(schedule_requests) == 1
    assert schedule_requests[0]["role"] == "mf_sub"
    assert schedule_requests[0]["profile_requirements"]["role"] == "mf_sub"
    assert "role" not in ticket["profile_requirements"]


def test_daemon_retries_terminal_run_with_same_profile_and_lineage(tmp_path):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        retry_policy={
            "attempt": 1,
            "max_attempts": 2,
            "on_crash": "retry_same_profile",
            "successor_required": True,
        },
    )
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)

    first = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "host_envelope": _guided_host_envelope(
                "worker-session-first",
                "worker-fence-first",
            ),
        }
    )
    registry.record_exit(
        first["run_id"],
        1,
        failure_category="process_crash",
    )
    retried = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "host_envelope": _guided_host_envelope(
                "worker-session-retry",
                "worker-fence-retry",
            ),
        }
    )

    canonical_run_id = "run-{}".format(ticket["ticket_id"])
    assert first["run_id"] == canonical_run_id
    assert retried["run_id"] == canonical_run_id + "-attempt-2"
    assert retried["run_id"] != first["run_id"]
    assert retried["profile_id"] == first["profile_id"] == "profile-codex-a"
    assert len(supervisor.starts) == 2
    assert supervisor.starts[0][0].profile == supervisor.starts[1][0].profile

    failed = registry.get_run(first["run_id"])
    successor = registry.get_run(retried["run_id"])
    assert failed is not None
    assert failed.state == "failed"
    assert successor is not None
    assert successor.run.parent_run_id == first["run_id"]
    assert successor.run.successor_of_run_id == first["run_id"]
    assert successor.run.config.profile_id == failed.run.config.profile_id

    privacy_fields = {
        key
        for key in first
        if key.startswith("raw_")
        or key.startswith("caller_")
        or key.startswith("transient_host_envelope_")
        or key in {"host_envelope_run_authority", "provider_output_suppressed"}
    }
    assert {key: retried[key] for key in privacy_fields} == {
        key: first[key] for key in privacy_fields
    }
    assert retried["raw_credentials_persisted"] is False
    assert retried["raw_prompt_persisted"] is False
    assert retried["raw_provider_output_persisted"] is False
    assert retried["provider_output_suppressed"] is True


def test_daemon_blocks_retry_exhaustion_before_schedule_or_spawn(
    tmp_path,
    monkeypatch,
):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        retry_policy={
            "attempt": 1,
            "max_attempts": 2,
            "on_crash": "retry_same_profile",
            "successor_required": True,
        },
    )
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)

    def admission_payload():
        return {
            "authority_selectors": selectors,
            "host_envelope": _guided_host_envelope(),
        }

    first = service._admit_governed_host_envelope_run(admission_payload())
    registry.record_exit(first["run_id"], 1, failure_category="process_crash")
    retried = service._admit_governed_host_envelope_run(admission_payload())
    registry.record_exit(retried["run_id"], 1, failure_category="process_crash")

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("retry exhaustion must stop before scheduling or staging")

    monkeypatch.setattr(service.scheduler, "schedule_run", unexpected_call)
    monkeypatch.setattr(service.host_envelope_store, "stage", unexpected_call)

    with pytest.raises(ServiceError, match="retry attempts exhausted"):
        service._admit_governed_host_envelope_run(admission_payload())

    assert len(supervisor.starts) == 2
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 2


def test_daemon_concurrent_retry_admits_one_successor_and_spawns_once(tmp_path):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile(max_concurrency=2))
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        retry_policy={
            "attempt": 1,
            "max_attempts": 2,
            "on_crash": "retry_same_profile",
            "successor_required": True,
        },
    )
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)

    first = service._admit_governed_host_envelope_run(
        {
            "authority_selectors": selectors,
            "host_envelope": _guided_host_envelope(
                "worker-session-first",
                "worker-fence-first",
            ),
        }
    )
    registry.record_exit(first["run_id"], 1, failure_category="process_crash")

    schedule_barrier = threading.Barrier(2)
    schedule_run = service.scheduler.schedule_run

    def contended_schedule_run(**kwargs):
        schedule_barrier.wait(timeout=5)
        return schedule_run(**kwargs)

    service.scheduler.schedule_run = contended_schedule_run
    outcomes = []
    errors = []

    def retry(suffix):
        try:
            outcomes.append(
                service._admit_governed_host_envelope_run(
                    {
                        "authority_selectors": selectors,
                        "host_envelope": _guided_host_envelope(
                            "worker-session-{}".format(suffix),
                            "worker-fence-{}".format(suffix),
                        ),
                    }
                )
            )
        except ServiceError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=retry, args=("a",)),
        threading.Thread(target=retry, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(outcomes) == 1
    assert len(errors) == 1
    assert "already admitted" in str(errors[0])
    assert outcomes[0]["run_id"] == first["run_id"] + "-attempt-2"
    assert len(supervisor.starts) == 2
    assert [item[0].run_id for item in supervisor.starts] == [
        first["run_id"],
        first["run_id"] + "-attempt-2",
    ]
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_leases WHERE run_id=?",
            (first["run_id"] + "-attempt-2",),
        ).fetchone()[0] == 1


def test_daemon_rejects_explicit_conflicting_profile_role(tmp_path):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(
        tmp_path,
        profile_role="qa",
    )
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)

    with pytest.raises(ServiceError, match="profile_requirements.role"):
        service._admit_governed_host_envelope_run(
            {
                "authority_selectors": selectors,
                "host_envelope": _guided_host_envelope(),
            }
        )

    assert supervisor.starts == []
    assert registry.get_run("run-{}".format(ticket["ticket_id"])) is None


@pytest.mark.parametrize(
    ("field_name", "stale_value"),
    (
        ("contract_execution_id", "cex-stale"),
        ("route_context_hash", "sha256:" + ("0" * 64)),
        ("expected_dispatch_identity_hash", "sha256:" + ("1" * 64)),
        ("profile_id", "profile-arbitrary"),
        ("backend_mode", "claude_cli"),
        ("role", "qa"),
        ("principal_id", "worker-arbitrary"),
    ),
)
def test_daemon_rejects_stale_or_mismatched_selectors_before_start(
    tmp_path, field_name, stale_value
):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(tmp_path)
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)
    stale = {**selectors, field_name: stale_value}

    with pytest.raises(ServiceError):
        service._admit_governed_host_envelope_run(
            {"authority_selectors": stale}
        )

    assert supervisor.starts == []
    assert registry.get_run("run-{}".format(ticket["ticket_id"])) is None


@pytest.mark.parametrize(
    "host_envelope",
    (
        None,
        {
            "env": {
                "AMING_WORKER_SESSION_TOKEN": "worker-session-invalid",
                "AMING_WORKER_FENCE_TOKEN": "worker-fence-invalid",
                "PATH": "/caller",
            }
        },
    ),
)
def test_daemon_rejects_missing_or_invalid_host_envelope_before_start(
    tmp_path, host_envelope
):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    ticket, selectors = _guided_ticket_and_selectors(tmp_path)
    service._contract_runtime_authority_resolver = lambda _request: dict(ticket)
    payload = {"authority_selectors": selectors}
    if host_envelope is not None:
        payload["host_envelope"] = host_envelope

    with pytest.raises(ServiceError, match="governed run could not be started"):
        service._admit_governed_host_envelope_run(payload)

    assert supervisor.starts == []
    failed = registry.get_run("run-{}".format(ticket["ticket_id"]))
    assert failed is not None
    assert failed.state == "failed"
    assert failed.lease is None


def test_daemon_rejects_caller_run_prompt_environment_before_resolution(tmp_path):
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServiceError, ServicePaths

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    registry.register_profile(_guided_profile())
    supervisor = _RecordingSupervisor(registry)
    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
        registry=registry,
        supervisor=supervisor,
    )
    _ticket, selectors = _guided_ticket_and_selectors(tmp_path)
    resolver_requests = []
    service._contract_runtime_authority_resolver = resolver_requests.append

    for field_name, value in (
        ("run", {"run_id": "caller-owned"}),
        ("prompt", "private prompt"),
        ("environment", {"AMING_WORKER_SESSION_TOKEN": "secret"}),
    ):
        with pytest.raises(ServiceError, match="unsupported fields"):
            service._admit_governed_host_envelope_run(
                {
                    "authority_selectors": selectors,
                    "host_envelope": _guided_host_envelope(),
                    field_name: value,
                }
            )

    assert resolver_requests == []
    assert supervisor.starts == []


def test_daemon_exposes_only_fixed_managed_profile_operations(tmp_path):
    from cli_agent_service.auth import ProfileAuthController
    from cli_agent_service.service import (
        CliAgentService,
        ServicePaths,
        request_service,
    )

    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    def ready_runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "Logged in", "")

    paths = ServicePaths.from_state_dir(tmp_path / "private-state")
    auth = ProfileAuthController(
        tmp_path / "private-state" / "profiles",
        codex_executable=str(executable),
        runner=ready_runner,
    )
    service = CliAgentService(paths, profile_auth_controller=auth)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_for(paths.socket_path)
        selector = {"profile_id": "profile-codex-a", "provider": "codex"}

        prepared = request_service(
            paths,
            "profile_login_prepare",
            payload=selector,
        )
        assert prepared["state"] == "login_in_progress"
        assert all(item["user_triggered"] for item in prepared["actions"])

        activated = request_service(
            paths,
            "profile_activate",
            payload=selector,
        )
        assert activated["activated"] is True
        assert activated["profile_registered"] is True

        listed = request_service(paths, "profile_list")
        assert [item["profile_id"] for item in listed["profiles"]] == [
            "profile-codex-a"
        ]

        refused = request_service(
            paths,
            "profile_auth_status",
            payload={**selector, "environment": {"CODEX_HOME": "/caller"}},
        )
        assert refused["ok"] is False
        assert refused["status"] == "invalid_request"
        assert "unsupported fields" in refused["error"]
    finally:
        if thread.is_alive():
            request_service(paths, "stop")
        thread.join(timeout=5)
        assert thread.is_alive() is False


def test_daemon_is_not_coupled_to_service_manager():
    for name in ("service.py", "health.py", "__main__.py"):
        source = (AGENT_DIR / "cli_agent_service" / name).read_text(encoding="utf-8")
        assert "service_manager" not in source.casefold()
        assert "ServiceManager" not in source


def test_macos_launch_agent_dry_run_contains_only_service_paths(tmp_path):
    script = REPO_ROOT / "scripts" / "install-cli-agent-service-macos.sh"
    completed = subprocess.run(
        [
            "sh", str(script), "--dry-run", "--python", sys.executable,
            "--repo-root", str(REPO_ROOT), "--state-dir", str(tmp_path / "state"),
        ],
        check=True, capture_output=True, text=True,
    )
    output = completed.stdout
    assert "dev.amingclaw.cli-agent-service" in output
    assert "<string>cli_agent_service</string>" in output
    assert "<string>start</string>" in output
    assert "ServiceManager" not in output
    assert "CODEX_HOME" not in output
    assert "CLAUDE_CONFIG_DIR" not in output
    assert "API_KEY" not in output
