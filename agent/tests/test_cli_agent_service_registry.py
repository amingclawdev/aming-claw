import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _profile(*, profile_id="profile-private", max_concurrency=1):
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
        version="3",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex",
            version="2",
            kind="codex_cli",
            executable_ref="managed:codex",
            capabilities=("stdio", "worktree"),
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-openai",
            version="4",
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode="codex_cli",
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:dev",
            version="5",
            provider="openai",
            ref_kind="provider_home",
        ),
        launcher_adapter=LauncherAdapter(
            launcher_id="launcher-codex-exec",
            version="2",
            environment_keys=("CODEX_HOME",),
        ),
        role_policy=RolePolicy(
            policy_id="policy-dev",
            version="7",
            roles=("dev",),
            project_ids=("aming-claw",),
            max_concurrency=max_concurrency,
            timeout_sec=300,
        ),
    )


def _run(run_id, profile, *, parent_run_id="", successor_of_run_id=""):
    from cli_agent_service.config import resolve_agent_config

    return resolve_agent_config(
        run_id=run_id,
        role="dev",
        project_id="aming-claw",
        profile=profile,
        created_at="2026-07-11T12:00:00Z",
        parent_run_id=parent_run_id,
        successor_of_run_id=successor_of_run_id,
        governance_refs={
            "runtime_context_id": "mfrctx-example",
            "timeline_ref": "timeline:123",
        },
    )


def _registry(tmp_path, **kwargs):
    from cli_agent_service.registry import AgentRegistry

    kwargs.setdefault("clock", lambda: NOW)
    return AgentRegistry(tmp_path / "private" / "cli-agent-registry.db", **kwargs)


def test_registry_uses_wal_and_private_file_modes(tmp_path):
    registry = _registry(tmp_path)

    conn = sqlite3.connect(registry.db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("SELECT value FROM registry_meta WHERE key='schema_version'").fetchone()[0] == "1"
    finally:
        conn.close()

    assert os.stat(registry.db_path).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(registry.db_path)).st_mode & 0o777 == 0o700


def test_profile_run_lineage_and_sanitized_refs_round_trip(tmp_path):
    registry = _registry(tmp_path)
    profile = _profile()
    first = _run("run-first", profile)
    successor = _run(
        "run-successor",
        profile,
        parent_run_id="run-first",
        successor_of_run_id="run-first",
    )

    registry.register_run(
        first,
        evidence_refs={
            "timeline_ref": "timeline:456",
            "graph_trace_id": "gqt-worker-example",
            "commit_sha": "a" * 40,
        },
    )
    registry.register_run(successor)

    stored_profile = registry.get_profile(profile.profile_id)
    stored_first = registry.get_run(first.run_id)
    stored_successor = registry.get_run(successor.run_id)
    assert stored_profile == profile
    assert stored_first.run.config.to_public_dict() == first.config.to_public_dict()
    assert stored_first.to_public_dict()["evidence_refs"] == {
        "commit_sha": "a" * 40,
        "graph_trace_id": "gqt-worker-example",
        "timeline_ref": "timeline:456",
    }
    assert stored_successor.run.parent_run_id == "run-first"
    assert stored_successor.run.successor_of_run_id == "run-first"
    assert stored_successor.to_public_dict()["operational_state_only"] is True
    assert stored_successor.to_public_dict()["governance_authority"] is False


@pytest.mark.parametrize(
    ("refs", "expected"),
    [
        ({"route_token": "opaque-route-token-value"}, "sanitized evidence"),
        ({"prompt_body": "private implementation request"}, "sanitized evidence"),
        ({"timeline_ref": "sk-private-credential-value"}, "raw secret"),
        ({"timeline_ref": "/shared/worktrees/private-transcript"}, "shared-volume path"),
    ],
)
def test_registry_rejects_credentials_tokens_prompts_and_paths(tmp_path, refs, expected):
    from cli_agent_service.registry import PersistenceRejectedError

    registry = _registry(tmp_path)
    run = _run("run-rejected", _profile())
    with pytest.raises(PersistenceRejectedError, match=expected):
        registry.register_run(run, evidence_refs=refs)

    with sqlite3.connect(registry.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


def test_profile_lease_acquisition_is_atomic_under_contention(tmp_path):
    from cli_agent_service.registry import AgentRegistry, LeaseConflictError

    registry = _registry(tmp_path)
    profile = _profile(max_concurrency=1)
    registry.register_run(_run("run-one", profile))
    registry.register_run(_run("run-two", profile))
    barrier = threading.Barrier(2)
    outcomes = []

    def acquire(run_id, owner_id):
        contender = AgentRegistry(registry.db_path, clock=lambda: NOW)
        barrier.wait()
        try:
            lease = contender.acquire_lease(run_id, owner_id, ttl_seconds=60)
        except LeaseConflictError:
            outcomes.append((run_id, "conflict"))
        else:
            outcomes.append((run_id, lease.status))

    threads = [
        threading.Thread(target=acquire, args=("run-one", "owner-one")),
        threading.Thread(target=acquire, args=("run-two", "owner-two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(status for _, status in outcomes) == ["active", "conflict"]
    with sqlite3.connect(registry.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_leases WHERE status='active'").fetchone()[0] == 1


def test_fresh_run_registration_is_atomic_under_same_run_contention(tmp_path):
    from cli_agent_service.registry import (
        AgentRegistry,
        RunRegistrationConflictError,
    )

    registry = _registry(tmp_path)
    profile = _profile(max_concurrency=2)
    run = _run("run-one-ticket", profile)
    barrier = threading.Barrier(2)
    outcomes = []

    def register():
        contender = AgentRegistry(registry.db_path, clock=lambda: NOW)
        barrier.wait(timeout=5)
        try:
            lease = contender.register_run_and_acquire_lease(
                run,
                "one-daemon-owner",
                ttl_seconds=60,
                require_new_run=True,
            )
        except RunRegistrationConflictError:
            outcomes.append("conflict")
        else:
            outcomes.append(lease.status)

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["active", "conflict"]
    with sqlite3.connect(registry.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE run_id='run-one-ticket'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_leases WHERE run_id='run-one-ticket'"
        ).fetchone()[0] == 1


def test_heartbeat_requires_lease_owner_and_persists_extension(tmp_path):
    from cli_agent_service.registry import LeaseNotOwnedError

    registry = _registry(tmp_path)
    run = _run("run-heartbeat", _profile())
    registry.register_run(run)
    lease = registry.acquire_lease(run.run_id, "owner-one", ttl_seconds=30)

    with pytest.raises(LeaseNotOwnedError):
        registry.heartbeat(run.run_id, "owner-two", ttl_seconds=60)
    renewed = registry.heartbeat(
        run.run_id,
        "owner-one",
        ttl_seconds=90,
        now=NOW + timedelta(seconds=10),
    )
    assert renewed.lease_id == lease.lease_id
    assert renewed.expires_at == "2026-07-11T12:01:40.000000Z"
    assert registry.get_run(run.run_id).last_heartbeat_at == "2026-07-11T12:00:10.000000Z"


def test_restart_reconciliation_classifies_all_host_states(tmp_path):
    from cli_agent_service.models import ProcessObservation

    identities = {
        101: "birth-live",
        102: "birth-reused",
        103: "birth-orphan",
    }
    registry = _registry(tmp_path, process_identity_reader=lambda pid: identities.get(pid))
    profile = _profile(max_concurrency=5)
    for run_id in ("run-live", "run-lost", "run-orphan", "run-completed", "run-failed"):
        registry.register_run(_run(run_id, profile))

    registry.acquire_lease("run-live", "owner-live", ttl_seconds=300)
    registry.record_process_start("run-live", pid=101, process_start_identity="birth-live")
    registry.acquire_lease("run-lost", "owner-lost", ttl_seconds=300)
    registry.record_process_start("run-lost", pid=102, process_start_identity="birth-original")
    registry.acquire_lease("run-orphan", "owner-orphan", ttl_seconds=5)
    registry.record_process_start("run-orphan", pid=103, process_start_identity="birth-orphan")
    registry.record_exit("run-completed", 0)
    registry.record_exit("run-failed", 17, failure_category="process_error")

    results = registry.reconcile_restart(now=NOW + timedelta(seconds=10))
    classifications = {result.run_id: result.classification for result in results}
    assert classifications == {
        "run-completed": "completed",
        "run-failed": "failed",
        "run-live": "live",
        "run-lost": "lost",
        "run-orphan": "orphaned",
    }
    assert registry.get_run("run-live").state == "live"
    assert registry.get_run("run-lost").lease is None
    assert registry.get_run("run-orphan").lease is None

    unknown = _registry(tmp_path / "unknown", process_identity_reader=lambda _pid: ProcessObservation(False, observable=False))
    unknown.register_run(_run("run-unknown", _profile()))
    unknown.acquire_lease("run-unknown", "owner", ttl_seconds=300)
    unknown.record_process_start("run-unknown", pid=999, process_start_identity="birth-unknown")
    assert unknown.reconcile_runs()[0].classification == "orphaned"


def test_registry_database_contains_no_governance_authority_columns(tmp_path):
    registry = _registry(tmp_path)
    registry.register_run(_run("run-operational-only", _profile()))

    with sqlite3.connect(registry.db_path) as conn:
        schema = "\n".join(
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        ).lower()
    assert "merge_authority" not in schema
    assert "close_authority" not in schema
    assert "next_legal_action" not in schema
    assert "private implementation request" not in json.dumps(registry.get_run("run-operational-only").to_public_dict())
