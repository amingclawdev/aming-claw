import json
import os
import sqlite3
import sys
import time
from pathlib import Path


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


def _fake_codex(tmp_path):
    path = tmp_path / "codex"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "args = sys.argv[1:]\n"
        "output = args[args.index('-o') + 1]\n"
        "prompt = sys.stdin.read()\n"
        "pathlib.Path(output).with_suffix('.ready').write_text('ready', encoding='utf-8')\n"
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


def _supervisor(tmp_path):
    from cli_agent_service.adapters.codex_cli import CodexCliAdapter
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.supervisor import CodexC0Supervisor

    registry = AgentRegistry(tmp_path / "registry" / "runs.db")
    supervisor = CodexC0Supervisor(
        registry,
        state_dir=tmp_path / "state",
        adapter=CodexCliAdapter(executable=str(_fake_codex(tmp_path))),
        heartbeat_interval_seconds=0.03,
        lease_ttl_seconds=2,
        cancellation_grace_seconds=0.1,
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
    handle = supervisor.start_run(run, prompt="private prompt", worktree=tmp_path)
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


def test_supervisor_cancels_owned_process_group(tmp_path):
    registry, supervisor = _supervisor(tmp_path)
    run = _run("run-cancel")
    handle = supervisor.start_run(run, prompt="sleep until cancelled", worktree=tmp_path)
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
