import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


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


def _wait_for(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for CLI Agent Service socket")


def test_host_envelope_store_is_single_use_ttl_bound_and_zeroizes_replacements():
    from cli_agent_service.launchers import HostEnvelopeStore

    clock = [100.0]
    store = HostEnvelopeStore(
        monotonic_clock=lambda: clock[0],
        wall_clock=lambda: datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
    )
    first_session, first_fence = _auth_values()
    first_envelope = _host_envelope(first_session, first_fence)
    first = store.stage("run-secure-envelope", first_envelope, ttl_seconds=10)
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
    store.stage("run-secure-envelope", replacement, ttl_seconds=2)
    replacement_buffers = tuple(
        store._entries["run-secure-envelope"].environment.values()
    )
    assert all(not value for value in first_buffers)
    assert store.consume("run-other") is None
    assert store.pending_count() == 1

    clock[0] += 3
    assert store.purge_expired() == 1
    assert all(not value for value in replacement_buffers)
    assert store.consume("run-secure-envelope") is None
    assert store.pending_count() == 0


def test_host_envelope_delivery_can_be_revoked_and_never_has_a_raw_read_api():
    import pytest

    from cli_agent_service.launchers import HostEnvelopeError, HostEnvelopeStore

    store = HostEnvelopeStore()
    session_token, fence_token = _auth_values()
    staged = store.stage(
        "run-revoked-envelope",
        _host_envelope(session_token, fence_token, suffix="revoked"),
    )
    staged_buffers = tuple(
        store._entries["run-revoked-envelope"].environment.values()
    )
    revoked = store.revoke(
        "run-revoked-envelope",
        envelope_ref=staged["envelope_ref"],
    )

    assert revoked["status"] == "revoked"
    assert all(not value for value in staged_buffers)
    assert store.consume("run-revoked-envelope") is None
    _assert_raw_absent(json.dumps(revoked, sort_keys=True), session_token, fence_token)
    with pytest.raises(HostEnvelopeError, match="does not match"):
        store.stage(
            "run-one",
            {
                **_host_envelope(*_auth_values(), suffix="wrong-run"),
                "run_id": "run-two",
            },
        )


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
    finally:
        request_service(paths, "stop")
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert store.pending_count() == 0
