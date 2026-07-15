import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _profile():
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    return AgentProfile(
        profile_id="profile-codex-inherited",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex",
            kind="codex_cli",
            executable_ref="managed:codex",
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-openai",
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode="codex_cli",
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:inherited",
            provider="openai",
            ref_kind="inherited_current",
        ),
        launcher_adapter=LauncherAdapter(launcher_id="launcher-codex-exec"),
        role_policy=RolePolicy(
            policy_id="policy-dev",
            roles=("dev",),
            project_ids=("aming-claw",),
            max_concurrency=1,
        ),
    )


def _run(run_id):
    from cli_agent_service.config import resolve_agent_config

    return resolve_agent_config(
        run_id=run_id,
        role="dev",
        project_id="aming-claw",
        profile=_profile(),
        created_at="2026-07-12T12:00:00Z",
    )


def _execution_ticket(run):
    import hashlib

    ticket_material = hashlib.sha256(run.run_id.encode("utf-8")).hexdigest()
    return {
        "schema_version": "cli_agent_execution_ticket.v1",
        "status": "issued",
        "issue_allowed": True,
        "ticket_id": "caet-" + ticket_material[:24],
        "ticket_hash": "sha256:" + ticket_material,
        "profile_requirements": {"profile_id": run.config.profile_id},
        "dispatch_identity": {
            "runtime_context_id": "mfrctx-supervisor-receipt"
        },
    }


def _auth_values_for_test():
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def _worker_envelope(session_token, fence_token, *, suffix):
    return {
        "schema_version": "mf_subagent_initial_join_host_envelope.v1",
        "runtime_context_id": "mfrctx-supervisor-{}".format(suffix),
        "task_id": "worker-supervisor-{}".format(suffix),
        "parent_task_id": "parent-supervisor-envelope",
        "worker_role": "mf_sub",
        "session_token_ref": "wstok-" + secrets.token_hex(16),
        "env": {
            "AMING_WORKER_SESSION_TOKEN": session_token,
            "AMING_WORKER_FENCE_TOKEN": fence_token,
        },
    }


def _assert_secret_absent(root, *values):
    encoded = tuple(value.encode() for value in values)
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        contents = path.read_bytes()
        if any(value in contents for value in encoded):
            raise AssertionError("raw worker auth was persisted to disk")


def _fake_codex(tmp_path):
    path = tmp_path / "codex"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "args = sys.argv[1:]\n"
        "output = args[args.index('-o') + 1]\n"
        "prompt = sys.stdin.read()\n"
        "if output != os.devnull:\n"
        "    pathlib.Path(output).with_suffix('.ready').write_text('ready', encoding='utf-8')\n"
        "if prompt.startswith('fail'):\n"
        "    sys.exit(7)\n"
        "if prompt.startswith('sleep'):\n"
        "    time.sleep(10)\n"
        "else:\n"
        "    time.sleep(0.15)\n"
        "pathlib.Path(output).write_text('completed:' + prompt, encoding='utf-8')\n"
        "print('{\"event\":\"completed\"}')\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _supervisor(
    tmp_path,
    *,
    run_receipt_sink=None,
    process_factory=None,
    host_envelope_store=None,
):
    from cli_agent_service.adapters.codex_cli import CodexCliAdapter
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.supervisor import CodexC0Supervisor

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    kwargs = {}
    if process_factory is not None:
        kwargs["process_factory"] = process_factory
    if host_envelope_store is not None:
        kwargs["host_envelope_store"] = host_envelope_store
    supervisor = CodexC0Supervisor(
        registry,
        state_dir=tmp_path / "state",
        adapter=CodexCliAdapter(executable=str(_fake_codex(tmp_path))),
        heartbeat_interval_seconds=0.03,
        lease_ttl_seconds=2,
        cancellation_grace_seconds=0.1,
        run_receipt_sink=run_receipt_sink,
        **kwargs,
    )
    return registry, supervisor


def _wait_running(registry, run_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = registry.get_run(run_id)
        if record and record.state == "running":
            return record
        time.sleep(0.02)
    raise AssertionError("run did not enter running state")


def _wait_probe_ready(state_dir, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tuple(Path(state_dir).glob("run-*/last-message.ready")):
            return
        time.sleep(0.02)
    raise AssertionError("fake Codex process did not install its signal handler")


def test_supervisor_owns_process_group_lease_heartbeat_and_receipt(tmp_path):
    registry, supervisor = _supervisor(tmp_path)
    run = _run("run-success")
    handle = supervisor.start_run(
        run,
        prompt="private prompt",
        worktree=tmp_path,
        execution_ticket=_execution_ticket(run),
    )
    receipt = handle.wait(timeout=5)
    assert receipt.status == "completed"
    assert receipt.exit_code == 0
    assert receipt.pid > 0
    assert receipt.process_group_id == receipt.pid
    assert receipt.command_hash.startswith("sha256:")
    assert receipt.output_hash.startswith("sha256:")
    public = receipt.to_public_dict()
    assert public["raw_prompt_stored"] is False
    assert public["raw_output_stored"] is False
    assert "private prompt" not in json.dumps(public)

    stored = registry.get_run(run.run_id)
    assert stored.state == "completed"
    assert stored.pid == receipt.pid
    assert stored.process_group_id == receipt.process_group_id
    assert stored.argv_hash == receipt.command_hash
    assert stored.lease is None
    with sqlite3.connect(registry.db_path) as conn:
        acquired_at, heartbeat_at = conn.execute(
            "SELECT acquired_at, heartbeat_at FROM agent_leases WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
    assert heartbeat_at >= acquired_at
    assert supervisor.active_run_ids() == ()
    run_receipts = supervisor.run_receipts(run.run_id)
    assert run_receipts[0]["state"] == "accepted"
    assert run_receipts[1]["state"] == "started"
    assert any(item["state"] == "heartbeat" for item in run_receipts)
    assert run_receipts[-1]["state"] == "completed"
    assert "private prompt" not in json.dumps(run_receipts)


def test_receipt_emitter_binds_unpinned_ticket_to_selected_run_profile(tmp_path):
    from cli_agent_service.evidence import hash_text

    _registry, supervisor = _supervisor(tmp_path)
    run = _run("run-unpinned-ticket")
    ticket = _execution_ticket(run)
    ticket["profile_requirements"] = {}

    emitter = supervisor._run_receipt_emitter(
        run,
        ticket,
        hash_text("selected profile command"),
    )
    receipt = emitter.emit("accepted", observed_at="2026-07-15T00:00:00Z")

    assert receipt.profile_id == run.config.profile_id


def test_receipt_emitter_rejects_pinned_ticket_profile_mismatch(tmp_path):
    from cli_agent_service.evidence import hash_text
    from cli_agent_service.supervisor import SupervisorError

    _registry, supervisor = _supervisor(tmp_path)
    run = _run("run-pinned-ticket-mismatch")
    ticket = _execution_ticket(run)
    ticket["profile_requirements"]["profile_id"] = "profile-other"

    with pytest.raises(
        SupervisorError,
        match="execution_ticket profile does not match the run",
    ):
        supervisor._run_receipt_emitter(
            run,
            ticket,
            hash_text("pinned profile command"),
        )


def test_managed_profile_launch_uses_exact_server_home_and_strips_provider_env(
    tmp_path,
    monkeypatch,
):
    from cli_agent_service.adapters.codex_cli import CodexCliAdapter
    from cli_agent_service.auth import ProfileAuthController
    from cli_agent_service.config import resolve_agent_config
    from cli_agent_service.profile_control import ManagedProfileControl
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.supervisor import CodexC0Supervisor

    executable = _fake_codex(tmp_path)

    def ready_runner(command, **_kwargs):
        args = tuple(command[1:])
        if args == ("plugin", "list", "--json"):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "installed": [
                            {
                                "pluginId": "aming-claw@aming-claw-local",
                                "version": "0.1.1+codex.20260713045902",
                                "installed": True,
                                "enabled": True,
                            }
                        ]
                    }
                ),
                "",
            )
        if args == ("mcp", "list", "--json"):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps([{"name": "aming-claw", "enabled": True}]),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "Logged in", "")

    registry = AgentRegistry(tmp_path / "managed-registry" / "runs.db")
    auth = ProfileAuthController(
        tmp_path / "managed-profiles",
        codex_executable=str(executable),
        runner=ready_runner,
    )
    control = ManagedProfileControl(
        registry,
        auth,
        tooling_runner=ready_runner,
    )
    control.prepare_login("profile-codex-managed")
    control.activate("profile-codex-managed")
    profile = registry.get_profile("profile-codex-managed")
    assert profile is not None
    run = resolve_agent_config(
        run_id="run-managed-profile",
        role="dev",
        project_id="aming-claw",
        profile=profile,
        created_at="2026-07-14T12:00:00Z",
    )

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-anthropic-key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/claude")
    monkeypatch.setenv("CODEX_HOME", "/ambient/codex")
    monkeypatch.setenv("UNRELATED_MARKER", "preserved")
    observed = {}

    def recording_factory(*args, **kwargs):
        observed.update(kwargs["env"])
        return subprocess.Popen(*args, **kwargs)

    supervisor = CodexC0Supervisor(
        registry,
        state_dir=tmp_path / "managed-state",
        adapter=CodexCliAdapter(executable=str(executable)),
        process_factory=recording_factory,
        managed_profile_home_resolver=control.resolve_profile_home,
        heartbeat_interval_seconds=0.03,
    )
    receipt = supervisor.start_run(
        run,
        prompt="managed prompt",
        worktree=tmp_path,
    ).wait(timeout=5)

    expected_home = str(
        auth.managed_profile_home("profile-codex-managed", "codex")
    )
    assert receipt.status == "completed"
    assert observed["CODEX_HOME"] == expected_home
    assert observed["UNRELATED_MARKER"] == "preserved"
    assert "OPENAI_API_KEY" not in observed
    assert "ANTHROPIC_API_KEY" not in observed
    assert "CLAUDE_CONFIG_DIR" not in observed


def test_inherited_current_launch_keeps_existing_provider_environment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-inherited-key")
    monkeypatch.setenv("CODEX_HOME", "/ambient/inherited-home")
    observed = {}

    def recording_factory(*args, **kwargs):
        observed.update(kwargs["env"])
        return subprocess.Popen(*args, **kwargs)

    _registry_value, supervisor = _supervisor(
        tmp_path,
        process_factory=recording_factory,
    )
    receipt = supervisor.start_run(
        _run("run-inherited-environment"),
        prompt="inherited prompt",
        worktree=tmp_path,
    ).wait(timeout=5)

    assert receipt.status == "completed"
    assert observed["OPENAI_API_KEY"] == "ambient-inherited-key"
    assert observed["CODEX_HOME"] == "/ambient/inherited-home"


def test_supervisor_cancels_owned_process_group(tmp_path):
    registry, supervisor = _supervisor(tmp_path)
    run = _run("run-cancel")
    handle = supervisor.start_run(
        run,
        prompt="sleep until cancelled",
        worktree=tmp_path,
        execution_ticket=_execution_ticket(run),
    )
    _wait_running(registry, run.run_id)
    _wait_probe_ready(tmp_path / "state")
    assert supervisor.cancel_run(run.run_id) is True
    receipt = handle.wait(timeout=5)
    assert receipt.status == "cancelled"
    assert receipt.exit_code == 130
    assert receipt.failure_category == "cancelled"
    stored = registry.get_run(run.run_id)
    assert stored.state == "failed"
    assert stored.failure_category == "cancelled"
    assert stored.lease is None
    assert supervisor.run_receipts(run.run_id)[-1]["state"] == "cancelled"
    assert supervisor.run_receipts(run.run_id)[-1]["exit_code"] == 130


def test_supervisor_emits_failed_run_receipt(tmp_path):
    registry, supervisor = _supervisor(tmp_path)
    run = _run("run-failed")

    receipt = supervisor.start_run(
        run,
        prompt="fail normally",
        worktree=tmp_path,
        execution_ticket=_execution_ticket(run),
    ).wait(timeout=5)

    assert receipt.status == "failed"
    assert receipt.exit_code == 7
    terminal = supervisor.run_receipts(run.run_id)[-1]
    assert terminal["state"] == "failed"
    assert terminal["exit_code"] == 7
    assert terminal["failure_category"] == "process_error"
    assert registry.get_run(run.run_id).state == "failed"


def test_supervisor_emits_spawn_failure_when_process_identity_is_unobservable(
    tmp_path,
):
    import pytest

    from cli_agent_service.supervisor import SupervisorError

    registry, supervisor = _supervisor(tmp_path)
    supervisor.process_identity_reader = lambda _pid: None
    run = _run("run-spawn-failure")

    with pytest.raises(SupervisorError, match="process identity is not observable"):
        supervisor.start_run(
            run,
            prompt="private prompt",
            worktree=tmp_path,
            execution_ticket=_execution_ticket(run),
        )

    receipts = supervisor.run_receipts(run.run_id)
    assert [item["state"] for item in receipts] == ["accepted", "failed"]
    assert receipts[-1]["failure_category"] == "spawn_error"
    assert receipts[-1]["exit_code"] == 127
    assert receipts[-1]["process_identity"] == {}
    stored = registry.get_run(run.run_id)
    assert stored.state == "failed"
    assert stored.failure_category == "spawn_error"


def test_supervisor_injects_single_run_host_auth_only_at_spawn(tmp_path, monkeypatch):
    from cli_agent_service.launchers import (
        HostEnvelopeStore,
        WORKER_FENCE_TOKEN_ENV,
        WORKER_SESSION_TOKEN_ENV,
    )

    session_token = secrets.token_urlsafe(32)
    fence_token = secrets.token_urlsafe(32)
    monkeypatch.setenv(WORKER_SESSION_TOKEN_ENV, secrets.token_urlsafe(32))
    monkeypatch.setenv(WORKER_FENCE_TOKEN_ENV, secrets.token_urlsafe(32))
    class TrackingStore(HostEnvelopeStore):
        last_delivery = None

        def consume(self, run_id, **kwargs):
            self.last_delivery = super().consume(run_id, **kwargs)
            return self.last_delivery

    store = TrackingStore()
    envelope = {
        "schema_version": "mf_subagent_initial_join_host_envelope.v1",
        "runtime_context_id": "mfrctx-supervisor-envelope",
        "task_id": "worker-supervisor-envelope",
        "parent_task_id": "parent-supervisor-envelope",
        "worker_role": "mf_sub",
        "session_token_ref": "wstok-" + secrets.token_hex(16),
        "env": {
            WORKER_SESSION_TOKEN_ENV: session_token,
            WORKER_FENCE_TOKEN_ENV: fence_token,
        },
    }
    observed = {}

    def recording_process_factory(*args, **kwargs):
        environment = kwargs["env"]
        observed["session_matches"] = hmac.compare_digest(
            environment.get(WORKER_SESSION_TOKEN_ENV, ""),
            session_token,
        )
        observed["fence_matches"] = hmac.compare_digest(
            environment.get(WORKER_FENCE_TOKEN_ENV, ""),
            fence_token,
        )
        observed["parent_environment"] = environment
        return subprocess.Popen(*args, **kwargs)

    registry, supervisor = _supervisor(
        tmp_path,
        process_factory=recording_process_factory,
        host_envelope_store=store,
    )
    store.stage(
        "run-host-envelope",
        envelope,
        lease_owner_id=supervisor.owner_id,
        ttl_seconds=30,
    )
    run = _run("run-host-envelope")
    handle = supervisor.start_run(
        run,
        prompt="Use the copy-safe runtime context and token reference.",
        worktree=tmp_path,
        execution_ticket=_execution_ticket(run),
        require_host_envelope=True,
    )

    assert observed["session_matches"] is True
    assert observed["fence_matches"] is True
    assert WORKER_SESSION_TOKEN_ENV not in observed["parent_environment"]
    assert WORKER_FENCE_TOKEN_ENV not in observed["parent_environment"]
    assert store.pending_count() == 0
    assert store.last_delivery is not None
    assert not store.last_delivery._environment
    receipt = handle.wait(timeout=5)
    assert receipt.status == "completed"
    assert receipt.output_hash == receipt.stdout_hash == receipt.stderr_hash
    assert not tuple((tmp_path / "state").glob("run-*/stdout.log"))
    assert not tuple((tmp_path / "state").glob("run-*/stderr.log"))

    public_artifacts = json.dumps(
        {
            "run": registry.get_run(run.run_id).to_public_dict(),
            "receipt": receipt.to_public_dict(),
            "run_receipts": supervisor.run_receipts(run.run_id),
        },
        sort_keys=True,
    )
    if session_token in public_artifacts or fence_token in public_artifacts:
        raise AssertionError("raw worker auth escaped into a public artifact")
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        contents = path.read_bytes()
        if session_token.encode() in contents or fence_token.encode() in contents:
            raise AssertionError("raw worker auth was persisted to disk")


def test_envelope_run_never_persists_malicious_child_stdout_or_stderr(tmp_path):
    from cli_agent_service.evidence import hash_text
    from cli_agent_service.launchers import HostEnvelopeStore

    marker = tmp_path / "malicious-child-ready"
    code = (
        "import os,pathlib,sys,time;"
        "sys.stdout.write(os.environ.get('AMING_WORKER_SESSION_TOKEN',''));"
        "sys.stdout.flush();"
        "sys.stderr.write(os.environ.get('AMING_WORKER_FENCE_TOKEN',''));"
        "sys.stderr.flush();"
        "pathlib.Path({!r}).write_text('ready',encoding='utf-8');"
        "sys.stdin.read();time.sleep(0.5)"
    ).format(str(marker))

    def malicious_factory(_command, **kwargs):
        return subprocess.Popen([sys.executable, "-c", code], **kwargs)

    store = HostEnvelopeStore()
    registry, supervisor = _supervisor(
        tmp_path,
        process_factory=malicious_factory,
        host_envelope_store=store,
    )
    run = _run("run-malicious-output")
    session_token = secrets.token_urlsafe(32)
    fence_token = secrets.token_urlsafe(32)
    store.stage(
        run.run_id,
        _worker_envelope(session_token, fence_token, suffix="malicious-output"),
        lease_owner_id=supervisor.owner_id,
    )

    handle = supervisor.start_run(
        run,
        prompt="Copy-safe prompt only.",
        worktree=tmp_path,
        execution_ticket=_execution_ticket(run),
        require_host_envelope=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.exists()
    assert registry.get_run(run.run_id).state == "running"
    assert not tuple((tmp_path / "state").rglob("stdout.log"))
    assert not tuple((tmp_path / "state").rglob("stderr.log"))
    _assert_secret_absent(tmp_path / "state", session_token, fence_token)

    receipt = handle.wait(timeout=5)
    empty_hash = hash_text("")
    assert receipt.output_hash == empty_hash
    assert receipt.stdout_hash == empty_hash
    assert receipt.stderr_hash == empty_hash
    assert not tuple((tmp_path / "state").rglob("stdout.log"))
    assert not tuple((tmp_path / "state").rglob("stderr.log"))
    _assert_secret_absent(tmp_path / "state", session_token, fence_token)


def test_lease_acquire_failure_revokes_and_zeroizes_staged_envelope(tmp_path):
    from cli_agent_service.launchers import HostEnvelopeStore
    from cli_agent_service.registry import LeaseConflictError

    store = HostEnvelopeStore()
    registry, supervisor = _supervisor(tmp_path, host_envelope_store=store)
    blocker = _run("run-lease-blocker")
    registry.register_run(blocker)
    registry.acquire_lease(blocker.run_id, "other-host-owner", ttl_seconds=30)

    run = _run("run-lease-rejected")
    store.stage(
        run.run_id,
        _worker_envelope(*_auth_values_for_test(), suffix="lease-rejected"),
        lease_owner_id=supervisor.owner_id,
    )
    buffers = tuple(store._entries[run.run_id].environment.values())

    with pytest.raises(LeaseConflictError):
        supervisor.start_run(
            run,
            prompt="Copy-safe prompt only.",
            worktree=tmp_path,
            require_host_envelope=True,
        )

    assert all(not value for value in buffers)
    assert store.pending_count() == 0
    assert registry.get_run(run.run_id).state == "registered"


def test_spawn_failure_discards_consumed_envelope(tmp_path):
    from cli_agent_service.launchers import HostEnvelopeStore
    from cli_agent_service.supervisor import SupervisorError

    class TrackingStore(HostEnvelopeStore):
        last_delivery = None

        def consume(self, run_id, **kwargs):
            self.last_delivery = super().consume(run_id, **kwargs)
            return self.last_delivery

    def failing_factory(*_args, **_kwargs):
        raise OSError("spawn unavailable")

    store = TrackingStore()
    registry, supervisor = _supervisor(
        tmp_path,
        process_factory=failing_factory,
        host_envelope_store=store,
    )
    run = _run("run-envelope-spawn-failure")
    store.stage(
        run.run_id,
        _worker_envelope(*_auth_values_for_test(), suffix="spawn-failure"),
        lease_owner_id=supervisor.owner_id,
    )

    with pytest.raises(SupervisorError, match="could not be started"):
        supervisor.start_run(
            run,
            prompt="Copy-safe prompt only.",
            worktree=tmp_path,
            require_host_envelope=True,
        )

    assert store.last_delivery is not None
    assert not store.last_delivery._environment
    assert store.pending_count() == 0
    assert registry.get_run(run.run_id).failure_category == "spawn_error"


@pytest.mark.parametrize("terminal_mode", ["cancel", "complete"])
def test_cancel_and_terminal_revoke_replacement_envelope(tmp_path, terminal_mode):
    from cli_agent_service.launchers import HostEnvelopeStore

    store = HostEnvelopeStore()
    registry, supervisor = _supervisor(tmp_path, host_envelope_store=store)
    run = _run("run-envelope-{}".format(terminal_mode))
    store.stage(
        run.run_id,
        _worker_envelope(*_auth_values_for_test(), suffix=terminal_mode),
        lease_owner_id=supervisor.owner_id,
    )
    handle = supervisor.start_run(
        run,
        prompt=("sleep until cancelled" if terminal_mode == "cancel" else "complete"),
        worktree=tmp_path,
        require_host_envelope=True,
    )
    _wait_running(registry, run.run_id)
    store.stage(
        run.run_id,
        _worker_envelope(*_auth_values_for_test(), suffix=terminal_mode + "-pending"),
        lease_owner_id=supervisor.owner_id,
    )
    buffers = tuple(store._entries[run.run_id].environment.values())

    if terminal_mode == "cancel":
        assert supervisor.cancel_run(run.run_id) is True
    receipt = handle.wait(timeout=5)

    assert receipt.status == ("cancelled" if terminal_mode == "cancel" else "completed")
    assert all(not value for value in buffers)
    assert store.pending_count() == 0


def test_restart_reconcile_emits_lost_run_receipt_from_durable_journal(tmp_path):
    from cli_agent_service.adapters.codex_cli import CodexCliAdapter
    from cli_agent_service.evidence import RunReceiptEmitter, hash_text
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.supervisor import CodexC0Supervisor

    registry = AgentRegistry(
        tmp_path / "registry" / "runs.db",
        process_identity_reader=lambda _pid: "different-process-start",
    )
    supervisor = CodexC0Supervisor(
        registry,
        state_dir=tmp_path / "state",
        adapter=CodexCliAdapter(executable=str(_fake_codex(tmp_path))),
    )
    run = _run("run-lost")
    registry.register_run(run)
    registry.acquire_lease(run.run_id, "test-owner", ttl_seconds=30)
    registry.record_process_start(
        run.run_id,
        pid=424242,
        process_start_identity="original-process-start",
        process_group_id=424242,
        argv_hash=hash_text("command"),
    )
    ticket = _execution_ticket(run)
    emitter = RunReceiptEmitter(
        run_id=run.run_id,
        ticket_id=ticket["ticket_id"],
        ticket_hash=ticket["ticket_hash"],
        profile_id=run.config.profile_id,
        runtime_context_id=ticket["dispatch_identity"]["runtime_context_id"],
        command_hash=hash_text("command"),
        sink=supervisor.receipt_journal.append,
    )
    emitter.emit("accepted", observed_at="2026-07-12T12:00:00Z")
    emitter.emit(
        "started",
        observed_at="2026-07-12T12:00:01Z",
        process_identity={
            "pid": 424242,
            "process_group_id": 424242,
            "process_start_identity_hash": hash_text("original-process-start"),
        },
    )

    result = supervisor.reconcile_restart()

    assert [(item.run_id, item.classification) for item in result] == [
        (run.run_id, "lost")
    ]
    assert supervisor.run_receipts(run.run_id)[-1]["state"] == "lost"


def test_restart_reconcile_observes_live_run_without_rewriting_identity(tmp_path):
    from cli_agent_service.registry import AgentRegistry

    registry, supervisor = _supervisor(tmp_path)
    run = _run("run-restart")
    handle = supervisor.start_run(run, prompt="sleep during restart probe", worktree=tmp_path)
    before = _wait_running(registry, run.run_id)
    restarted_registry = AgentRegistry(registry.db_path)
    result = restarted_registry.reconcile_runs()
    assert [(item.run_id, item.classification) for item in result] == [(run.run_id, "live")]
    after = restarted_registry.get_run(run.run_id)
    assert after.run.config.profile_id == before.run.config.profile_id
    assert after.run.config.credential_ref == before.run.config.credential_ref
    supervisor.cancel_run(run.run_id)
    assert handle.wait(timeout=5).status == "cancelled"
