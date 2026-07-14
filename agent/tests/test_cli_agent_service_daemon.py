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


def _guided_profile(*, profile_id="profile-codex-a", backend_mode="codex_cli"):
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
            ref_kind="inherited_current",
        ),
        launcher_adapter=LauncherAdapter(launcher_id="launcher-codex-guided"),
        role_policy=RolePolicy(
            policy_id="policy-codex-guided",
            roles=("mf_sub",),
            project_ids=("aming-claw",),
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


def _guided_ticket_and_selectors(tmp_path, *, profile_id="profile-codex-a"):
    from governance.contract_state_runtime import build_cli_agent_execution_ticket

    route_identity = {
        "route_id": "route-guided-daemon",
        "route_context_hash": "sha256:" + ("b" * 64),
        "prompt_contract_id": "rprompt-guided-daemon",
        "prompt_contract_hash": "sha256:" + ("c" * 64),
        "route_token_ref": "rtok-guided-daemon",
        "visible_injection_manifest_hash": "sha256:" + ("d" * 64),
    }
    action = {
        "id": "worker_dispatch",
        "action": "dispatch_bounded_worker",
        "stage_id": "dispatch",
        "line_id": "observer_dispatch_bounded_workers",
        "evidence_kind": "dispatch_bounded_worker",
        "owner_role": "observer",
        "worker_role": "mf_sub",
        "runtime_context_id": "mfrctx-guided-daemon",
        "task_id": "task-guided-daemon",
        "worker_id": "worker-guided-daemon",
        "worker_slot_id": "worker-guided-daemon",
        "observer_command_id": "command-guided-daemon",
        "parent_task_id": "AC-GUIDED-DAEMON",
        "target_project_root": str(tmp_path),
        "branch_ref": "refs/heads/codex/guided-daemon",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "merge_queue_id": "mq-guided-daemon",
        "owned_files": ["agent/owned.py"],
        **route_identity,
        "profile_requirements": {
            "profile_id": profile_id,
            "role": "mf_sub",
            "harness": "codex",
            "provider": "openai",
            "model": "gpt-5.4-codex",
        },
        "retry_policy": {"attempt": 1, "max_attempts": 1},
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
        "authority_decision_source": "contract_runtime_completed_dispatch_line",
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
        "profile_id": profile_id,
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
