import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))
LEASE_OWNER = "cli-agent-host-test"


def _auth_values():
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def _host_envelope(session_token, fence_token, *, suffix="one"):
    return {
        "schema_version": "mf_subagent_initial_join_host_envelope.v1",
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-secure-envelope-{}".format(suffix),
        "task_id": "worker-secure-envelope-{}".format(suffix),
        "parent_task_id": "parent-secure-envelope",
        "worker_role": "mf_sub",
        "worker_id": "worker-secure-envelope-{}".format(suffix),
        "worker_slot_id": "slot-secure-envelope-{}".format(suffix),
        "session_token_ref": "wstok-{}".format(secrets.token_hex(16)),
        "fence_token_redacted": True,
        "route_identity": {
            "route_id": "route-secure-envelope-{}".format(suffix),
            "route_context_hash": "sha256:" + ("a" * 64),
            "prompt_contract_id": "rprompt-secure-envelope-{}".format(suffix),
            "prompt_contract_hash": "sha256:" + ("b" * 64),
        },
        "env": {
            "AMING_WORKER_SESSION_TOKEN": session_token,
            "AMING_WORKER_FENCE_TOKEN": fence_token,
        },
        "raw_tokens_persisted_to_timeline": False,
    }


def _assert_raw_absent(serialized, *raw_values):
    if any(value in serialized for value in raw_values):
        raise AssertionError("raw worker auth escaped into a public artifact")


def _assert_raw_absent_from_tree(root, *raw_values):
    encoded = tuple(value.encode() for value in raw_values)
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        contents = path.read_bytes()
        if any(value in contents for value in encoded):
            raise AssertionError("raw worker auth was persisted to disk")


def _wait_for(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for CLI Agent Service socket")


def _daemon_run(executable):
    from cli_agent_service.config import resolve_agent_config
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    profile = AgentProfile(
        profile_id="profile-daemon-envelope",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-daemon-envelope",
            kind="codex_cli",
            executable_ref="path:{}".format(executable),
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-daemon-envelope",
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
        launcher_adapter=LauncherAdapter(launcher_id="launcher-daemon-envelope"),
        role_policy=RolePolicy(
            policy_id="policy-daemon-envelope",
            roles=("dev",),
            project_ids=("aming-claw",),
        ),
    )
    return resolve_agent_config(
        run_id="run-daemon-envelope",
        role="dev",
        project_id="aming-claw",
        profile=profile,
        created_at="2026-07-12T12:00:00Z",
    )


def _execution_ticket(run, runtime_context_id):
    import hashlib

    digest = hashlib.sha256(run.run_id.encode()).hexdigest()
    return {
        "schema_version": "cli_agent_execution_ticket.v1",
        "status": "issued",
        "issue_allowed": True,
        "ticket_id": "caet-" + digest[:24],
        "ticket_hash": "sha256:" + digest,
        "profile_requirements": {"profile_id": run.config.profile_id},
        "dispatch_identity": {"runtime_context_id": runtime_context_id},
    }


def _malicious_codex(path):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "output = args[args.index('-o') + 1]\n"
        "session = os.environ.get('AMING_WORKER_SESSION_TOKEN', '')\n"
        "fence = os.environ.get('AMING_WORKER_FENCE_TOKEN', '')\n"
        "pathlib.Path('worker-ready.json').write_text(json.dumps({\n"
        "    'session_present': bool(session), 'fence_present': bool(fence)\n"
        "}), encoding='utf-8')\n"
        "sys.stdout.write(session)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write(fence)\n"
        "sys.stderr.flush()\n"
        "sys.stdin.read()\n"
        "time.sleep(0.6)\n"
        "pathlib.Path(output).write_text('completed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_host_envelope_store_is_single_use_ttl_bound_and_zeroizes_replacements():
    from cli_agent_service.launchers import HostEnvelopeStore

    clock = [100.0]
    store = HostEnvelopeStore(
        monotonic_clock=lambda: clock[0],
        wall_clock=lambda: datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
    )
    first_session, first_fence = _auth_values()
    first_envelope = _host_envelope(first_session, first_fence)
    first = store.stage(
        "run-secure-envelope",
        first_envelope,
        lease_owner_id=LEASE_OWNER,
        ttl_seconds=10,
    )
    first_buffers = tuple(
        store._entries["run-secure-envelope"].environment.values()
    )

    assert "env" not in first_envelope
    assert first["status"] == "staged"
    assert first["session_token_redacted"] is True
    assert first["fence_token_redacted"] is True
    assert first["raw_worker_auth_exposed"] is False
    _assert_raw_absent(json.dumps(first, sort_keys=True), first_session, first_fence)

    second_session, second_fence = _auth_values()
    replacement = _host_envelope(second_session, second_fence, suffix="two")
    store.stage(
        "run-secure-envelope",
        replacement,
        lease_owner_id=LEASE_OWNER,
        ttl_seconds=2,
    )
    replacement_buffers = tuple(
        store._entries["run-secure-envelope"].environment.values()
    )
    assert all(not value for value in first_buffers)
    assert store.consume(
        "run-other",
        lease_owner_id=LEASE_OWNER,
        lease_id="lease-test-other",
    ) is None
    assert store.pending_count() == 1

    clock[0] += 3
    assert store.purge_expired() == 1
    assert all(not value for value in replacement_buffers)
    assert store.consume(
        "run-secure-envelope",
        lease_owner_id=LEASE_OWNER,
        lease_id="lease-test-expired",
    ) is None
    assert store.pending_count() == 0


def test_host_envelope_delivery_can_be_revoked_and_never_has_a_raw_read_api():
    import pytest

    from cli_agent_service.launchers import HostEnvelopeError, HostEnvelopeStore

    store = HostEnvelopeStore()
    session_token, fence_token = _auth_values()
    staged = store.stage(
        "run-revoked-envelope",
        _host_envelope(session_token, fence_token, suffix="revoked"),
        lease_owner_id=LEASE_OWNER,
    )
    staged_buffers = tuple(
        store._entries["run-revoked-envelope"].environment.values()
    )
    revoked = store.revoke(
        "run-revoked-envelope",
        envelope_ref=staged["envelope_ref"],
        lease_owner_id=LEASE_OWNER,
    )

    assert revoked["status"] == "revoked"
    assert all(not value for value in staged_buffers)
    assert store.consume(
        "run-revoked-envelope",
        lease_owner_id=LEASE_OWNER,
        lease_id="lease-test-revoked",
    ) is None
    _assert_raw_absent(json.dumps(revoked, sort_keys=True), session_token, fence_token)
    with pytest.raises(HostEnvelopeError, match="does not match"):
        store.stage(
            "run-one",
            {
                **_host_envelope(*_auth_values(), suffix="wrong-run"),
                "run_id": "run-two",
            },
            lease_owner_id=LEASE_OWNER,
        )


def test_host_envelope_consume_rejects_wrong_lease_owner_and_zeroizes():
    import pytest

    from cli_agent_service.launchers import HostEnvelopeError, HostEnvelopeStore

    store = HostEnvelopeStore()
    store.stage(
        "run-owner-bound",
        _host_envelope(*_auth_values(), suffix="owner-bound"),
        lease_owner_id=LEASE_OWNER,
    )
    buffers = tuple(store._entries["run-owner-bound"].environment.values())

    with pytest.raises(HostEnvelopeError, match="owner does not match"):
        store.consume(
            "run-owner-bound",
            lease_owner_id="cli-agent-host-other",
            lease_id="lease-owner-mismatch",
        )

    assert all(not value for value in buffers)
    assert store.pending_count() == 0


def test_service_host_envelope_operation_is_public_safe_and_host_local(tmp_path):
    from cli_agent_service.launchers import HostEnvelopeStore
    from cli_agent_service.service import (
        CliAgentService,
        ServicePaths,
        request_service,
    )

    paths = ServicePaths.from_state_dir(tmp_path / "state")
    store = HostEnvelopeStore()
    service = CliAgentService(paths, host_envelope_store=store)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    _wait_for(paths.socket_path)

    session_token, fence_token = _auth_values()
    envelope = _host_envelope(session_token, fence_token, suffix="service")
    request_payload = {
        "action": "stage",
        "run_id": "run-service-envelope",
        "host_envelope": envelope,
        "ttl_seconds": 30,
    }
    stop_buffers = ()
    try:
        response = request_service(
            paths,
            "host_envelope",
            payload=request_payload,
        )
        assert response["ok"] is True
        assert response["status"] == "staged"
        assert response["runtime_context_id"] == "mfrctx-secure-envelope-service"
        assert response["raw_worker_auth_exposed"] is False
        assert "env" not in envelope
        _assert_raw_absent(
            json.dumps(response, sort_keys=True),
            session_token,
            fence_token,
        )

        retrieve = request_service(
            paths,
            "host_envelope",
            payload={
                "action": "consume",
                "run_id": "run-service-envelope",
            },
        )
        assert retrieve == {
            "error": "unsupported host envelope action",
            "ok": False,
            "status": "invalid_request",
        }
        assert store.pending_count() == 1

        revoked = request_service(
            paths,
            "host_envelope",
            payload={
                "action": "revoke",
                "run_id": "run-service-envelope",
                "envelope_ref": response["envelope_ref"],
            },
        )
        assert revoked["status"] == "revoked"
        assert store.pending_count() == 0
        persisted_status = paths.status_path.read_text(encoding="utf-8")
        _assert_raw_absent(persisted_status, session_token, fence_token)
        request_service(
            paths,
            "host_envelope",
            payload={
                "action": "stage",
                "run_id": "run-service-stop",
                "host_envelope": _host_envelope(
                    *_auth_values(),
                    suffix="service-stop",
                ),
            },
        )
        stop_buffers = tuple(
            store._entries["run-service-stop"].environment.values()
        )
    finally:
        request_service(paths, "stop")
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert all(not value for value in stop_buffers)
    assert store.pending_count() == 0


def test_real_daemon_atomically_starts_envelope_run_without_output_leak(tmp_path):
    from cli_agent_service.evidence import RunReceiptJournal, hash_text
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import ServicePaths, request_service

    state_dir = tmp_path / "daemon-state"
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    executable = _malicious_codex(worker_dir / "codex")
    run = _daemon_run(executable)
    runtime_context_id = "mfrctx-daemon-envelope"
    session_token, fence_token = _auth_values()
    envelope = _host_envelope(
        session_token,
        fence_token,
        suffix="daemon",
    )
    paths = ServicePaths.from_state_dir(state_dir)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(AGENT_DIR)
    daemon = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_service",
            "start",
            "--state-dir",
            str(state_dir),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(paths.socket_path)
        health = request_service(paths, "health")
        assert health["accepting_agent_runs"] is True
        response = request_service(
            paths,
            "start_host_envelope_run",
            payload={
                "run": run.to_public_dict(),
                "worktree": str(worker_dir),
                "prompt": "Use only the copy-safe runtime references.",
                "execution_ticket": _execution_ticket(run, runtime_context_id),
                "evidence_refs": {
                    "runtime_context_id": runtime_context_id,
                    "session_token_ref": envelope["session_token_ref"],
                },
                "host_envelope": envelope,
                "ttl_seconds": 30,
            },
        )
        assert response == {
            "ok": True,
            "run_id": run.run_id,
            "status": "started",
        }
        _assert_raw_absent(
            json.dumps(response, sort_keys=True),
            session_token,
            fence_token,
        )

        marker = worker_dir / "worker-ready.json"
        _wait_for(marker)
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        assert marker_payload == {
            "fence_present": True,
            "session_present": True,
        }
        assert not tuple(state_dir.rglob("stdout.log"))
        assert not tuple(state_dir.rglob("stderr.log"))
        _assert_raw_absent_from_tree(state_dir, session_token, fence_token)

        registry = AgentRegistry(state_dir / "registry" / "runs.db")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = registry.get_run(run.run_id)
            if record and record.state == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("daemon worker did not reach terminal state")

        journal = RunReceiptJournal(state_dir / "supervisor" / "run-receipts")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipts = journal.receipts(run.run_id)
            if receipts and receipts[-1]["state"] == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("daemon worker terminal receipt was not emitted")
        assert receipts[-1]["state"] == "completed"
        assert receipts[-1]["output_hash"] == hash_text("")
        assert not tuple(state_dir.rglob("stdout.log"))
        assert not tuple(state_dir.rglob("stderr.log"))
        _assert_raw_absent_from_tree(state_dir, session_token, fence_token)
    finally:
        try:
            request_service(paths, "stop")
        except Exception:
            pass
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.terminate()
            daemon.wait(timeout=5)
        daemon_stdout, daemon_stderr = daemon.communicate(timeout=5)
    if session_token.encode() in daemon_stdout or fence_token.encode() in daemon_stdout:
        raise AssertionError("raw worker auth escaped through daemon stdout")
    if session_token.encode() in daemon_stderr or fence_token.encode() in daemon_stderr:
        raise AssertionError("raw worker auth escaped through daemon stderr")
