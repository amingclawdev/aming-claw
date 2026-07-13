from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from agent.cli_agent_service.adapters.codex_desktop import (
    CodexDesktopAdapter,
    DesktopHostAdapterError,
    _stable_hash,
)
from agent.cli_agent_service.service import (
    CliAgentService,
    ServiceError,
    ServicePaths,
    ServiceUnavailableError,
)
from agent.governance.contract_state_runtime import build_cli_agent_execution_ticket


def _ticket_inputs() -> dict[str, object]:
    launch_identity: dict[str, object] = {
        "project_id": "aming-claw",
        "backlog_id": "AC-DESKTOP",
        "task_id": "desktop-worker",
        "worker_id": "desktop-worker",
        "worker_slot_id": "desktop-slot",
        "observer_command_id": "observer-desktop-1",
        "parent_task_id": "desktop-parent",
        "runtime_context_id": "mfrctx-desktop",
        "worker_role": "mf_sub",
        "worktree_path": "/tmp/desktop-worker",
        "branch_ref": "refs/heads/desktop-worker",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "merge_queue_id": "mq-desktop",
        "owned_files": ["agent/observer_runtime.py"],
        "route_id": "route-desktop",
        "route_context_hash": "sha256:" + "1" * 64,
        "prompt_contract_id": "prompt-desktop",
        "prompt_contract_hash": "sha256:" + "2" * 64,
        "route_token_ref": "rtref-desktop",
        "visible_injection_manifest_hash": "sha256:" + "3" * 64,
    }
    profile = {"profile_id": "codex-desktop", "harness": "codex"}
    retry = {"attempt": 1, "max_attempts": 2}
    authority = {
        "source_of_authority": "ContractRuntime",
        "authority_decision_source": "contract_runtime_completed_dispatch_line",
        "project_id": "aming-claw",
        "backlog_id": "AC-DESKTOP",
        "contract_execution_id": "cex-desktop",
        "contract_revision_id": "sha256:" + "4" * 64,
        "execution_state_revision": 7,
        "execution_state_hash": "sha256:" + "5" * 64,
        "runtime_guide_hash": "sha256:" + "6" * 64,
        "readiness_state": "contract_active",
        "next_legal_action": {
            "id": "worker_dispatch",
            "action": "dispatch_bounded_worker",
            "target_project_root": "/tmp/desktop-worker",
            **launch_identity,
            "profile_requirements": profile,
            "retry_policy": retry,
        },
    }
    return {
        "contract_runtime_current_state": authority,
        "launch_identity": launch_identity,
        "profile_requirements": profile,
        "retry_policy": retry,
        "expected_execution_state_revision": 7,
        "expected_execution_state_hash": "sha256:" + "5" * 64,
    }


def _ticket() -> dict[str, object]:
    ticket = build_cli_agent_execution_ticket(**_ticket_inputs())
    assert ticket["status"] == "issued"
    return ticket


def _admission_payload() -> dict[str, object]:
    ticket = _ticket()
    dispatch = ticket["dispatch_identity"]
    return {
        "host_kind": "codex_desktop",
        "project_id": dispatch["project_id"],
        "backlog_id": dispatch["backlog_id"],
        "contract_execution_id": ticket["contract_execution_id"],
        "runtime_context_id": dispatch["runtime_context_id"],
        "task_id": dispatch["task_id"],
        "worker_id": dispatch["worker_id"],
        "worker_slot_id": dispatch["worker_slot_id"],
        "observer_command_id": dispatch["observer_command_id"],
        "expected_execution_state_revision": ticket["execution_state_revision"],
        "expected_execution_state_hash": ticket["execution_state_hash"],
        "expected_dispatch_identity_hash": ticket["dispatch_identity_hash"],
        "now_iso": "2026-07-13T12:00:01Z",
    }


def _ready_adapter() -> tuple[CodexDesktopAdapter, dict[str, object], dict[str, object]]:
    adapter = CodexDesktopAdapter()
    adapter.register_host(
        host_id="desktop-host-1",
        capabilities=["acknowledge_execution_ticket", "join_runtime_context"],
        now_iso="2026-07-13T12:00:00Z",
    )
    adapter.heartbeat(
        host_id="desktop-host-1",
        heartbeat_id="heartbeat-1",
        now_iso="2026-07-13T12:00:01Z",
    )
    ticket = _ticket()
    adapter._admit_service_execution_ticket(
        canonical_execution_ticket=ticket,
        now_iso="2026-07-13T12:00:01Z",
    )
    ack = adapter.acknowledge_execution_ticket(
        host_id="desktop-host-1",
        execution_ticket=ticket,
        run_id="desktop-run-1",
        now_iso="2026-07-13T12:00:02Z",
    )
    return adapter, ticket, ack


def test_public_adapter_cannot_admit_a_caller_built_matching_ticket() -> None:
    adapter = CodexDesktopAdapter()
    ticket = _ticket()

    with pytest.raises(DesktopHostAdapterError, match="canonical service authority"):
        adapter.admit_execution_ticket(
            execution_ticket=ticket,
            canonical_execution_ticket=ticket,
        )


def test_public_service_rejects_caller_fabricated_authority_and_ticket(tmp_path) -> None:
    resolver_calls: list[dict[str, object]] = []

    def resolver(request):
        resolver_calls.append(dict(request))
        return _ticket()

    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
    )
    service._contract_runtime_authority_resolver = resolver
    payload = _admission_payload()
    inputs = _ticket_inputs()
    payload.update(
        {
            "contract_runtime_current_state": inputs[
                "contract_runtime_current_state"
            ],
            "launch_identity": inputs["launch_identity"],
            "profile_requirements": inputs["profile_requirements"],
            "retry_policy": inputs["retry_policy"],
            "execution_ticket": _ticket(),
        }
    )

    with pytest.raises(ServiceError, match="unsupported authority fields"):
        service._dispatch(
            {"operation": "desktop_execution_ticket_admit", "payload": payload}
        )
    assert resolver_calls == []


def test_service_resolves_canonical_ticket_and_preserves_desktop_lifecycle(tmp_path) -> None:
    ticket = _ticket()
    resolver_requests: list[dict[str, object]] = []

    def resolver(request):
        resolver_requests.append(dict(request))
        return deepcopy(ticket)

    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "state"),
    )
    service._contract_runtime_authority_resolver = resolver
    register, _ = service._dispatch(
        {
            "operation": "desktop_host_register",
            "payload": {
                "host_kind": "codex_desktop",
                "host_id": "desktop-host-1",
                "capabilities": [
                    "acknowledge_execution_ticket",
                    "join_runtime_context",
                ],
                "now_iso": "2026-07-13T12:00:00Z",
            },
        }
    )
    assert register["status"] == "registered"
    heartbeat, _ = service._dispatch(
        {
            "operation": "desktop_host_heartbeat",
            "payload": {
                "host_kind": "codex_desktop",
                "host_id": "desktop-host-1",
                "heartbeat_id": "heartbeat-1",
                "now_iso": "2026-07-13T12:00:01Z",
            },
        }
    )
    assert heartbeat["status"] == "healthy"
    admitted, _ = service._dispatch(
        {
            "operation": "desktop_execution_ticket_admit",
            "payload": _admission_payload(),
        }
    )
    assert admitted["status"] == "admitted"
    assert admitted["execution_ticket"] == ticket
    assert resolver_requests == [
        {
            key: value
            for key, value in _admission_payload().items()
            if key not in {"host_kind", "now_iso"}
        }
    ]
    ack, _ = service._dispatch(
        {
            "operation": "desktop_execution_ticket_ack",
            "payload": {
                "host_kind": "codex_desktop",
                "host_id": "desktop-host-1",
                "execution_ticket": admitted["execution_ticket"],
                "run_id": "desktop-run-1",
                "now_iso": "2026-07-13T12:00:02Z",
            },
        }
    )
    joined, _ = service._dispatch(
        {
            "operation": "desktop_runtime_join",
            "payload": {
                "host_kind": "codex_desktop",
                "ticket_ack": ack,
                "actual_host_worker_id": "desktop-host-worker-1",
                "worker_session_id": "codex-session-1",
                "worker_transcript_ref": "codex:codex-session-1",
                "session_token_ref": "wstok-copy-safe-ref",
                "observer_command_id": "observer-desktop-1",
                "worker_slot_id": "desktop-slot",
                "launch_text_hash": "sha256:" + "7" * 64,
                "now_iso": "2026-07-13T12:00:03Z",
            },
        }
    )
    assert joined["status"] == "joined"
    assert joined["dispatch_identity_hash"] == ticket["dispatch_identity_hash"]
    assert joined["registered_host_adapter_spawn"]["observer_command_id"] == (
        "observer-desktop-1"
    )
    assert "session_token_surrogate" not in joined["registered_host_adapter_spawn"]


@pytest.mark.parametrize(
    "omitted_fields",
    [
        pytest.param((), id="matching-both"),
        pytest.param(
            ("expected_execution_state_hash",),
            id="omit-state-hash",
        ),
        pytest.param(
            ("expected_dispatch_identity_hash",),
            id="omit-dispatch-hash",
        ),
        pytest.param(
            (
                "expected_execution_state_hash",
                "expected_dispatch_identity_hash",
            ),
            id="omit-both",
        ),
    ],
)
def test_matching_or_omitted_optional_authority_hashes_use_resolver(
    tmp_path,
    omitted_fields,
) -> None:
    ticket = _ticket()
    resolver_requests: list[dict[str, object]] = []

    def resolver(request):
        resolver_requests.append(dict(request))
        return deepcopy(ticket)

    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "optional-hashes"),
    )
    service._contract_runtime_authority_resolver = resolver
    payload = _admission_payload()
    for field in omitted_fields:
        payload.pop(field)

    admitted, _ = service._dispatch(
        {"operation": "desktop_execution_ticket_admit", "payload": payload}
    )

    assert admitted["status"] == "admitted"
    assert admitted["execution_ticket"] == ticket
    assert resolver_requests == [
        {
            key: value
            for key, value in payload.items()
            if key not in {"host_kind", "now_iso"}
        }
    ]


@pytest.mark.parametrize(
    "field",
    [
        "expected_execution_state_hash",
        "expected_dispatch_identity_hash",
    ],
)
def test_supplied_mismatched_optional_authority_hash_fails_closed(
    tmp_path,
    field,
) -> None:
    resolver_calls: list[dict[str, object]] = []

    def resolver(request):
        resolver_calls.append(dict(request))
        return _ticket()

    service = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / field),
    )
    service._contract_runtime_authority_resolver = resolver
    payload = _admission_payload()
    payload[field] = "sha256:" + "f" * 64

    with pytest.raises(ServiceError, match="stale or mismatched authority"):
        service._dispatch(
            {"operation": "desktop_execution_ticket_admit", "payload": payload}
        )
    assert resolver_calls == [
        {
            key: value
            for key, value in payload.items()
            if key not in {"host_kind", "now_iso"}
        }
    ]


def test_missing_or_stale_server_authority_fails_closed(tmp_path) -> None:
    payload = _admission_payload()

    unavailable = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "unavailable"),
    )
    unavailable._contract_runtime_authority_resolver = lambda _request: (
        _ for _ in ()
    ).throw(ServiceUnavailableError("authority unavailable"))
    with pytest.raises(ServiceUnavailableError, match="authority unavailable"):
        unavailable._dispatch(
            {"operation": "desktop_execution_ticket_admit", "payload": payload}
        )

    stale_ticket = _ticket()
    stale_ticket["execution_state_revision"] = 8
    stale_ticket["ticket_hash"] = _stable_hash(
        {key: value for key, value in stale_ticket.items() if key != "ticket_hash"}
    )
    stale = CliAgentService(
        ServicePaths.from_state_dir(tmp_path / "stale"),
    )
    stale._contract_runtime_authority_resolver = lambda _request: stale_ticket
    with pytest.raises(ServiceError, match="stale or mismatched authority"):
        stale._dispatch(
            {"operation": "desktop_execution_ticket_admit", "payload": payload}
        )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("worker_id", "other-worker"),
        ("worker_slot_id", "other-slot"),
        ("observer_command_id", "other-command"),
    ],
)
def test_ticket_hash_binds_complete_dispatch_identity(field, replacement) -> None:
    inputs = _ticket_inputs()
    baseline = build_cli_agent_execution_ticket(**inputs)
    authority = deepcopy(inputs["contract_runtime_current_state"])
    launch = deepcopy(inputs["launch_identity"])
    authority["next_legal_action"][field] = replacement
    launch[field] = replacement
    changed = build_cli_agent_execution_ticket(
        contract_runtime_current_state=authority,
        launch_identity=launch,
        profile_requirements=inputs["profile_requirements"],
        retry_policy=inputs["retry_policy"],
    )
    assert changed["status"] == "issued"
    assert changed["dispatch_identity_hash"] != baseline["dispatch_identity_hash"]
    assert changed["ticket_hash"] != baseline["ticket_hash"]

    rejected = build_cli_agent_execution_ticket(
        contract_runtime_current_state=inputs["contract_runtime_current_state"],
        launch_identity={**inputs["launch_identity"], field: replacement},
    )
    assert rejected["status"] == "rejected"
    assert field in {item["field"] for item in rejected["mismatches"]}


def test_ack_and_join_races_remain_atomic_and_idempotent() -> None:
    adapter = CodexDesktopAdapter()
    for host_id in ("desktop-host-1", "desktop-host-2"):
        adapter.register_host(
            host_id=host_id,
            capabilities=["acknowledge_execution_ticket", "join_runtime_context"],
            now_iso="2026-07-13T12:00:00Z",
        )
        adapter.heartbeat(
            host_id=host_id,
            heartbeat_id="heartbeat-" + host_id[-1],
            now_iso="2026-07-13T12:00:01Z",
        )
    ticket = _ticket()
    adapter._admit_service_execution_ticket(canonical_execution_ticket=ticket)
    barrier = Barrier(2)

    def acknowledge(host_id):
        barrier.wait()
        try:
            return adapter.acknowledge_execution_ticket(
                host_id=host_id,
                execution_ticket=ticket,
                run_id="desktop-run-1",
                now_iso="2026-07-13T12:00:02Z",
            )
        except DesktopHostAdapterError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        ack_results = list(pool.map(acknowledge, ("desktop-host-1", "desktop-host-2")))
    acks = [result for result in ack_results if isinstance(result, dict)]
    assert len(acks) == 1
    ack = acks[0]
    assert adapter.acknowledge_execution_ticket(
        host_id=ack["host_id"],
        execution_ticket=ticket,
        run_id="desktop-run-1",
        now_iso="2026-07-13T12:00:03Z",
    ) == ack

    join_barrier = Barrier(2)

    def join(worker_id):
        join_barrier.wait()
        try:
            return adapter.join_runtime_context(
                ticket_ack=ack,
                actual_host_worker_id=worker_id,
                worker_session_id="session-" + worker_id[-1],
                worker_transcript_ref="codex:session-" + worker_id[-1],
                session_token_ref="wstok-copy-safe-ref",
                observer_command_id="observer-desktop-1",
                worker_slot_id="desktop-slot",
                launch_text_hash="sha256:" + "7" * 64,
                now_iso="2026-07-13T12:00:03Z",
            )
        except DesktopHostAdapterError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        join_results = list(pool.map(join, ("host-worker-1", "host-worker-2")))
    assert len([result for result in join_results if isinstance(result, dict)]) == 1
