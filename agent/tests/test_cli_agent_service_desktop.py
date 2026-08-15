from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from agent.cli_agent_service.adapters.codex_desktop import (
    CodexDesktopAdapter,
    DesktopHostAdapterError,
    _stable_hash,
)
from agent.cli_agent_service.guided_runtime import (
    GuidedRuntimeDispatchError,
    orchestrate_runtime_context_graph_continuation,
    orchestrate_runtime_context_host_startup,
    orchestrate_runtime_context_implementation_continuation,
)
from agent.cli_agent_service.service import (
    CliAgentService,
    ServiceError,
    ServicePaths,
    ServiceUnavailableError,
    mcp_application_mapping_blocks,
    unwrap_mcp_application_response,
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


def test_ticket_binds_distinct_queue_claim_and_contract_runtime_dispatch_identity() -> None:
    inputs = _ticket_inputs()
    authority = deepcopy(inputs["contract_runtime_current_state"])
    launch = deepcopy(inputs["launch_identity"])
    authority["next_legal_action"]["observer_command_id"] = "cex-desktop"
    launch.update(
        {
            "observer_command_id": "cex-desktop",
            "contract_runtime_observer_command_id": "cex-desktop",
            "claimed_observer_command_id": "cmd-desktop-claimed",
        }
    )

    ticket = build_cli_agent_execution_ticket(
        contract_runtime_current_state=authority,
        launch_identity=launch,
        profile_requirements=inputs["profile_requirements"],
        retry_policy=inputs["retry_policy"],
    )

    assert ticket["status"] == "issued"
    assert ticket["dispatch_identity"]["observer_command_id"] == "cex-desktop"
    assert ticket["dispatch_identity"][
        "contract_runtime_observer_command_id"
    ] == "cex-desktop"
    assert ticket["dispatch_identity"][
        "claimed_observer_command_id"
    ] == "cmd-desktop-claimed"

    rejected = build_cli_agent_execution_ticket(
        contract_runtime_current_state=authority,
        launch_identity={
            **launch,
            "contract_runtime_observer_command_id": "cex-other",
        },
    )
    assert rejected["status"] == "rejected"
    assert "contract_runtime_observer_command_id" in {
        item["field"] for item in rejected["mismatches"]
    }

    missing_contract_identity = build_cli_agent_execution_ticket(
        contract_runtime_current_state=authority,
        launch_identity={
            key: value
            for key, value in launch.items()
            if key != "contract_runtime_observer_command_id"
        },
    )
    assert missing_contract_identity["status"] == "rejected"
    assert (
        "claimed observer command requires explicit ContractRuntime dispatch identity"
        in missing_contract_identity["errors"]
    )


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


def _runtime_context_host_guide() -> dict[str, object]:
    route = {
        "route_id": "route-host",
        "route_context_hash": "sha256:" + "1" * 64,
        "prompt_contract_id": "prompt-host",
        "prompt_contract_hash": "sha256:" + "2" * 64,
        "route_token_ref": "rtok-host",
        "visible_injection_manifest_hash": "sha256:" + "3" * 64,
    }
    receipt = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-host",
        "task_id": "host-worker",
        "parent_task_id": "cex-host",
        "contract_execution_id": "cex-host",
        "contract_hash": "sha256:" + "4" * 64,
        "context_hash": "sha256:" + "5" * 64,
        "worker_role": "mf_sub",
        "worker_id": "governed-worker",
        "worker_slot_id": "governed-slot",
        "target_project_root": "/tmp/host-worker",
        "session_token": "<read from worker env>",
        "session_token_ref": "wstok-allocation-old",
        "fence_token": "<read from worker env>",
        "session_token_env": "AMING_WORKER_SESSION_TOKEN",
        "fence_token_env": "AMING_WORKER_FENCE_TOKEN",
        "event_type": "mf_subagent_read_receipt",
        "event_kind": "contract_context_read_receipt",
        "status": "accepted",
        "read_receipt_hash": "<worker-computed-read-receipt-hash>",
        "launch_text_hash": "<launch-text-sha256-if-known>",
        "contract_context_read_receipt": {
            "project_id": "aming-claw",
            "actor_role": "mf_sub",
            "actor_session_principal": "<server-verified worker session principal>",
            "contract_execution_id": "cex-host",
            "runtime_context_id": "mfrctx-host",
            "task_id": "host-worker",
            "parent_task_id": "cex-host",
            "worker_slot_id": "governed-slot",
            "route_token_ref": "rtok-host",
            "context_hash": "sha256:" + "5" * 64,
            "contract_hash": "sha256:" + "4" * 64,
            "acknowledged_at": "<worker-generated ISO-8601 timestamp>",
            "receipt_hash": "<worker-computed-read-receipt-hash>",
            "read_receipt_hash": "<worker-computed-read-receipt-hash>",
            **route,
        },
        "payload": {
            "runtime_context_id": "mfrctx-host",
            "read_receipt_hash": "<worker-computed-read-receipt-hash>",
            "launch_text_hash": "<launch-text-sha256-if-known>",
            "contract_context_read_receipt": {
                "acknowledged_at": "<worker-generated ISO-8601 timestamp>",
                "receipt_hash": "<worker-computed-read-receipt-hash>",
                "read_receipt_hash": "<worker-computed-read-receipt-hash>",
            },
        },
        **route,
    }
    application = {
        "project_id": "aming-claw",
        "runtime_context_id": "mfrctx-host",
        "task_id": "host-worker",
        "actionable_payloads": {
            "session_token_initial_join_submission": {
                "mcp_tool": "runtime_context_session_token_initial_join",
                "copy_safe_body": {
                    # The live compatibility guide omits adapter-realized
                    # project and allocation host identity fields here.
                    "runtime_context_id": "mfrctx-host",
                    "task_id": "host-worker",
                    "parent_task_id": "cex-host",
                    "contract_execution_id": "cex-host",
                    "target_project_root": "/tmp/host-worker",
                    "worker_id": "governed-worker",
                    "worker_slot_id": "governed-slot",
                    "allocation_owner": "observer-allocation",
                    "worker_session_id": "<actual Desktop/Codex worker session id>",
                    "session_token_ref": "wstok-allocation-old",
                    **route,
                    "reason": "<operator reason>",
                    "ttl_seconds": 3600,
                },
            },
            "read_receipt_facade_payload_skeleton": {
                "mcp_tool": "runtime_context_read_receipt",
                "copy_safe_body": receipt,
            },
            "startup_facade_payload_skeleton": {
                "legacy_tool": "parallel_branch_startup",
                "copy_safe_body": {
                    # The real startup guide likewise relies on adapter injection.
                    "runtime_context_id": "mfrctx-host",
                    "task_id": "host-worker",
                    "parent_task_id": "cex-host",
                    "worker_role": "mf_sub",
                    "worker_id": "governed-worker",
                    "worker_slot_id": "governed-slot",
                    "agent_id": "governed-worker",
                    "allocation_owner": "observer-allocation",
                    "observer_allocation_owner": "observer-allocation",
                    "branch": "refs/heads/host-worker",
                    "branch_ref": "refs/heads/host-worker",
                    "base_commit": "a" * 40,
                    "target_head_commit": "a" * 40,
                    "merge_queue_id": "mq-host",
                    "owned_files": [
                        "src/reminders.js",
                        "tests/reminders.test.mjs",
                    ],
                    "observer_command_id": "<claimed execute_backlog_row command id>",
                    "target_project_root": "/tmp/host-worker",
                    "session_token": "<read from worker env>",
                    "session_token_ref": "wstok-allocation-old",
                    "fence_token": "<read from worker env>",
                    "worker_session_id": "<actual worker-owned session id>",
                    "worker_transcript_ref": "<host transcript ref>",
                    "worker_transcript_path": "<local transcript path if available>",
                    "harness_type": "codex",
                    "filer_principal": "<actual worker principal filing startup>",
                    "actual_host_worker_id": "governed-worker",
                    "host_startup_id": "<host startup event/thread id>",
                    "host_session_id": "<host session id>",
                    "actual_cwd": "/tmp/host-worker",
                    "actual_git_root": "/tmp/host-worker",
                    "head_commit": "<worker worktree HEAD after launch>",
                    "read_receipt_hash": "<accepted-read-receipt-hash>",
                    "read_receipt_event_id": "<accepted-read-receipt-event-id>",
                    "worker_session_lifecycle_policy": {
                        "not_an_mcp_field": "<must be filtered before validation>"
                    },
                    **route,
                },
            },
        },
    }
    return {
        "content": [{"type": "text", "text": json.dumps(application)}]
    }


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True, "status": "direct"},
        {"structuredContent": {"ok": True, "status": "structured"}},
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"ok": True, "status": "text"}),
                }
            ]
        },
    ],
)
def test_mcp_application_response_unwraps_real_host_shapes(response) -> None:
    application = unwrap_mcp_application_response(response)

    assert application["ok"] is True
    assert application["status"] in {"direct", "structured", "text"}


def test_mcp_application_response_rejects_non_json_text_content() -> None:
    with pytest.raises(ServiceError, match="JSON object"):
        unwrap_mcp_application_response(
            {"content": [{"type": "text", "text": "not-json"}]}
        )


def test_runtime_context_host_orchestration_is_uninterrupted_and_private() -> None:
    session_token = "raw-session-host-orchestration"
    fence_token = "raw-fence-host-orchestration"
    call_names: list[str] = []
    live_bodies: list[dict[str, object]] = []
    call_summaries: list[dict[str, object]] = []

    def call_tool(name, body):
        call_names.append(name)
        live_bodies.append(body)
        assert "<" not in json.dumps(body, sort_keys=True)
        if name == "runtime_context_session_token_initial_join":
            assert body["project_id"] == "aming-claw"
            assert body["agent_id"] == "governed-worker"
            assert body["actual_host_worker_id"] == "governed-worker"
            assert body["worker_session_id"] == "codex-thread-42"
            assert body["host_session_id"] == "codex-thread-42"
            assert body["host_startup_id"] == "startup-thread-42"
            assert "allocation_owner" not in body
            return {
                "structuredContent": {
                    "ok": True,
                    "status": "session_token_initial_join_issued",
                    "worker_session_token_ref": "wstok-joined-authoritative",
                    "host_envelope": {
                        "session_token_ref": "wstok-joined-authoritative",
                        "env": {
                            "AMING_WORKER_SESSION_TOKEN": session_token,
                            "AMING_WORKER_FENCE_TOKEN": fence_token,
                        },
                    },
                }
            }
        if name == "runtime_context_read_receipt":
            call_summaries.append(deepcopy(body))
            assert body["session_token"] == session_token
            assert body["fence_token"] == fence_token
            assert body["session_token_ref"] == "wstok-joined-authoritative"
            assert body["contract_context_read_receipt"]["receipt_hash"] == (
                body["read_receipt_hash"]
            )
            assert body["contract_context_read_receipt"][
                "actor_session_principal"
            ] == "codex-thread-42"
            assert body["contract_context_read_receipt"]["acknowledged_at"] == (
                "2026-08-09T14:00:00Z"
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": True,
                                "status": "accepted",
                                "read_receipt_hash": body["read_receipt_hash"],
                                "read_receipt_event_id": "timeline:90210",
                            }
                        ),
                    }
                ]
            }
        call_summaries.append(deepcopy(body))
        assert name == "parallel_branch_startup"
        assert body["project_id"] == "aming-claw"
        assert body["session_token"] == session_token
        assert body["fence_token"] == fence_token
        assert body["session_token_ref"] == "wstok-joined-authoritative"
        assert body["agent_id"] == "governed-worker"
        assert body["actual_host_worker_id"] == "governed-worker"
        assert body["worker_session_id"] == "codex-thread-42"
        assert body["host_session_id"] == "codex-thread-42"
        assert body["host_startup_id"] == "startup-thread-42"
        assert body["observer_command_id"] == "observer-desktop-1"
        assert body["owned_files"] == [
            "src/reminders.js",
            "tests/reminders.test.mjs",
        ]
        assert body["read_receipt_event_id"] == "timeline:90210"
        assert "worker_transcript_path" not in body
        assert body["worker_transcript_ref"] == "codex:codex-thread-42"
        assert "worker_session_lifecycle_policy" not in body
        assert "worker_slot_id" not in body
        return {"ok": True, "status": "passed", "startup_event_ref": "timeline:90211"}

    result = orchestrate_runtime_context_host_startup(
        worker_guide=_runtime_context_host_guide(),
        tool_caller=call_tool,
        host_identity={
            "worker_session_id": "codex-thread-42",
            "host_startup_id": "startup-thread-42",
            "observer_command_id": "observer-desktop-1",
            "head_commit": "b" * 40,
        },
        reason="same-invocation host startup",
        now_iso="2026-08-09T14:00:00Z",
    )

    assert result["ok"] is True
    assert result["sequence"] == call_names
    assert result["session_token_ref"] == "wstok-joined-authoritative"
    assert result["uninterrupted_same_invocation"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert session_token not in serialized
    assert fence_token not in serialized
    assert "host_envelope_incomplete" not in serialized
    assert "tool_error" not in serialized
    # The exact mutable request objects and tool results were scrubbed after
    # the sequence; deep copies made inside the fake prove what was invoked.
    assert all(session_token not in json.dumps(body) for body in live_bodies)
    assert all(fence_token not in json.dumps(body) for body in live_bodies)
    assert call_summaries[0]["session_token_ref"] == "wstok-joined-authoritative"
    assert call_summaries[1]["session_token_ref"] == "wstok-joined-authoritative"


@pytest.mark.parametrize("field_name", ["agent_id", "actual_host_worker_id"])
def test_runtime_context_host_orchestration_rejects_host_identity_override(
    field_name,
) -> None:
    calls = []

    with pytest.raises(
        GuidedRuntimeDispatchError,
        match="host identity conflicts at {}".format(field_name),
    ):
        orchestrate_runtime_context_host_startup(
            worker_guide=_runtime_context_host_guide(),
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "observer_command_id": "observer-desktop-1",
                field_name: "caller-guessed-worker",
                "head_commit": "b" * 40,
            },
        )

    assert calls == []


@pytest.mark.parametrize("guide_value", [None, "<unresolved allocation identity>"])
def test_runtime_context_host_orchestration_requires_guide_host_identity(
    guide_value,
) -> None:
    guide = _runtime_context_host_guide()
    application = json.loads(guide["content"][0]["text"])
    startup = application["actionable_payloads"][
        "startup_facade_payload_skeleton"
    ]["copy_safe_body"]
    for field_name in ("agent_id", "actual_host_worker_id"):
        if guide_value is None:
            startup.pop(field_name)
        else:
            startup[field_name] = guide_value
    guide["content"][0]["text"] = json.dumps(application)
    calls = []

    with pytest.raises(
        GuidedRuntimeDispatchError,
        match="missing agent_id, actual_host_worker_id",
    ):
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "observer_command_id": "observer-desktop-1",
                "agent_id": "caller-guessed-worker",
                "actual_host_worker_id": "caller-guessed-worker",
                "head_commit": "b" * 40,
            },
        )

    assert calls == []


def test_runtime_context_host_orchestration_requires_declared_observer_command() -> None:
    calls = []

    with pytest.raises(
        GuidedRuntimeDispatchError,
        match="requires observer_command_id",
    ):
        orchestrate_runtime_context_host_startup(
            worker_guide=_runtime_context_host_guide(),
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "head_commit": "b" * 40,
            },
        )

    assert calls == []


def test_runtime_context_host_orchestration_rejects_observer_command_placeholder() -> None:
    calls = []

    with pytest.raises(
        GuidedRuntimeDispatchError,
        match="contains placeholder observer_command_id",
    ):
        orchestrate_runtime_context_host_startup(
            worker_guide=_runtime_context_host_guide(),
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "observer_command_id": "<caller observer command>",
                "head_commit": "b" * 40,
            },
        )

    assert calls == []


def test_runtime_context_host_orchestration_rejects_observer_command_conflict() -> None:
    guide = _runtime_context_host_guide()
    application = json.loads(guide["content"][0]["text"])
    application["actionable_payloads"]["startup_facade_payload_skeleton"][
        "copy_safe_body"
    ]["observer_command_id"] = "observer-guide"
    guide["content"][0]["text"] = json.dumps(application)
    calls = []

    with pytest.raises(
        GuidedRuntimeDispatchError,
        match="host identity conflicts at observer_command_id",
    ):
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "observer_command_id": "observer-caller",
                "head_commit": "b" * 40,
            },
        )

    assert calls == []


def test_runtime_context_host_orchestration_rejects_placeholders_before_call() -> None:
    guide = _runtime_context_host_guide()
    application = json.loads(guide["content"][0]["text"])
    application["actionable_payloads"]["session_token_initial_join_submission"][
        "copy_safe_body"
    ]["ttl_seconds"] = "<unrealized ttl>"
    guide["content"][0]["text"] = json.dumps(application)
    calls = []

    with pytest.raises(GuidedRuntimeDispatchError, match="unresolved ttl_seconds"):
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "head_commit": "b" * 40,
            },
        )

    assert calls == []


def test_runtime_context_host_orchestration_rejects_unknown_host_guess_zero_call() -> None:
    guide = _runtime_context_host_guide()
    application = json.loads(guide["content"][0]["text"])
    application["actionable_payloads"]["read_receipt_facade_payload_skeleton"][
        "copy_safe_body"
    ]["payload"]["undocumented_host_guess"] = "<must-not-be-host-realized>"
    guide["content"][0]["text"] = json.dumps(application)
    private_guess = "host-injected-undocumented-value"
    calls = []

    with pytest.raises(
        GuidedRuntimeDispatchError, match="undocumented_host_guess"
    ) as raised:
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "codex-thread-42",
                "head_commit": "b" * 40,
                "undocumented_host_guess": private_guess,
            },
        )

    assert calls == []
    assert private_guess not in str(raised.value)


def test_runtime_context_host_orchestration_preserves_server_error_object() -> None:
    server_error = {
        "ok": False,
        "status": "rejected_before_server",
        "code": -32603,
        "message": "missing required property project_id",
        "error": {
            "code": "mcp_schema_validation_failed",
            "field": "project_id",
        },
        "error_alias": "schema_validation",
    }

    result = orchestrate_runtime_context_host_startup(
        worker_guide=_runtime_context_host_guide(),
        tool_caller=lambda _name, _body: {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps(server_error)}],
        },
        host_identity={
            "worker_session_id": "codex-thread-42",
            "observer_command_id": "observer-desktop-1",
            "head_commit": "b" * 40,
        },
    )

    assert result["ok"] is False
    assert result["code"] == -32603
    assert result["error"] == server_error["error"]
    assert result["error_alias"] == "schema_validation"
    assert result["server_response"]["message"] == server_error["message"]
    serialized = json.dumps(result, sort_keys=True)
    assert "tool_error" not in serialized
    assert "host_envelope_incomplete" not in serialized


def _live_refreshing_host_startup_inputs(
    precursor_tool="runtime_context_session_token_rejoin",
):
    route = {
        "route_id": "route-live-host-continuation",
        "route_context_hash": "sha256:" + "7" * 64,
        "prompt_contract_id": "prompt-live-host-continuation",
        "prompt_contract_hash": "sha256:" + "8" * 64,
        "route_token_ref": "rtok-live-host-continuation",
        "visible_injection_manifest_hash": "sha256:" + "9" * 64,
    }
    scope = {
        "project_id": "aming-claw",
        "backlog_id": "AC-LIVE-HOST-CONTINUATION",
        "contract_execution_id": "cex-live-host-continuation",
        "runtime_context_id": "mfrctx-live-host-continuation",
        "task_id": "live-host-continuation-worker",
        "parent_task_id": "cex-live-host-continuation",
        "target_project_root": "/tmp/live-host-continuation",
        "worker_id": "live-host-continuation-worker",
        "worker_slot_id": "live-host-continuation-worker",
        "agent_id": "live-host-continuation-worker",
        "actual_host_worker_id": "live-host-continuation-worker",
        **route,
    }
    precursor_body = {
        **scope,
        "worker_session_id": "<actual worker session id>",
        "host_session_id": "<actual host session id>",
        "host_startup_id": "<actual host startup id>",
        "session_token_ref": "wstok-live-before",
        "reason": "<operator reason>",
    }
    guide = {
        "project_id": scope["project_id"],
        "backlog_id": scope["backlog_id"],
        "contract_execution_id": scope["contract_execution_id"],
        "selected_role": "mf_sub",
        "selected_work_type": "parallel_worker",
        "route_token_ref": route["route_token_ref"],
        "host_precursor_required": True,
        "execution_sequence": [
            "host_precursor_action",
            "refresh_onboard_route_guide",
            "canonical_contract_action",
        ],
        "host_precursor_action": {
            "mcp_tool": precursor_tool,
            "copy_safe_body": precursor_body,
        },
    }
    receipt_body = {
        **scope,
        "session_token": "<host-realized session_token>",
        "fence_token": "<host-realized fence_token>",
        "session_token_ref": "wstok-live-after",
        "read_receipt_hash": "<launch text hash>",
        "launch_text_hash": "<launch text hash>",
    }
    startup_body = {
        **scope,
        "owned_files": ["src/reminders.js", "tests/reminders.test.mjs"],
        "session_token": "<host-realized session_token>",
        "fence_token": "<host-realized fence_token>",
        "session_token_ref": "wstok-live-after",
        "worker_session_id": "<actual worker session id>",
        "host_session_id": "<actual host session id>",
        "host_startup_id": "<actual host startup id>",
        "filer_principal": "<actual worker principal>",
        "head_commit": "<worker head commit>",
        "read_receipt_hash": "<accepted receipt hash>",
        "read_receipt_event_id": "<accepted receipt event>",
        "worker_transcript_ref": "<host transcript ref>",
        "actual_cwd": scope["target_project_root"],
        "actual_git_root": scope["target_project_root"],
        "harness_type": "codex",
    }
    return guide, receipt_body, startup_body


def _server_recovery_selected_host_guide(mode):
    guide, _receipt_body, _startup_body = _live_refreshing_host_startup_inputs()
    precursor = guide.pop("host_precursor_action")
    guide.pop("host_precursor_required", None)
    guide.pop("execution_sequence", None)
    body = deepcopy(precursor["copy_safe_body"])
    initial_body = deepcopy(body)
    initial_body["session_token_ref"] = "wstok-allocation-initial"
    recovery = {
        "eligible": True,
        "mode": mode,
        "context_status": "running",
        "blockers": [],
    }
    actionable_payloads = {
        "session_token_initial_join_submission": {
            "mcp_tool": "runtime_context_session_token_initial_join",
            "copy_safe_body": initial_body,
        },
        "session_token_rejoin_eligibility": deepcopy(recovery),
    }
    if mode == "safe_ref_prestartup_reissue":
        expected_tool = "runtime_context_session_token_reissue"
        submission_name = "session_token_reissue_submission"
        renewal_name = "reissue"
    else:
        expected_tool = "runtime_context_session_token_rejoin"
        submission_name = "session_token_rejoin_submission"
        renewal_name = "rejoin"
    submission = {
        "mcp_tool": expected_tool,
        "copy_safe_body": body,
    }
    actionable_payloads[submission_name] = deepcopy(submission)
    actionable_payloads["session_renewal_hints"] = {
        renewal_name: deepcopy(submission)
    }
    guide["session_token_rejoin_eligibility"] = deepcopy(recovery)
    guide["actionable_payloads"] = actionable_payloads
    return guide, expected_tool


@pytest.mark.parametrize(
    ("mode", "expected_tool"),
    [
        (
            "pre_lineage_bootstrap_auth_only",
            "runtime_context_session_token_rejoin",
        ),
        ("active_context_auth_only", "runtime_context_session_token_rejoin"),
        (
            "bounded_post_lineage_replacement_auth_only",
            "runtime_context_session_token_rejoin",
        ),
        (
            "safe_ref_prestartup_reissue",
            "runtime_context_session_token_reissue",
        ),
    ],
)
def test_host_orchestration_selects_server_declared_auth_recovery(
    mode, expected_tool
) -> None:
    guide, projected_tool = _server_recovery_selected_host_guide(mode)
    assert projected_tool == expected_tool
    calls = []

    def call_tool(name, body):
        calls.append((name, deepcopy(body)))
        return {
            "ok": False,
            "status": "rejected",
            "message": "bounded test stop after selector",
        }

    result = orchestrate_runtime_context_host_startup(
        worker_guide=guide,
        tool_caller=call_tool,
        host_identity={
            "worker_session_id": "live-host-session",
            "host_startup_id": "live-host-startup",
            "head_commit": "a" * 40,
        },
        reason="select the server-declared recovery",
    )

    assert result["ok"] is False
    assert [name for name, _body in calls] == [expected_tool]
    if expected_tool == "runtime_context_session_token_reissue":
        assert calls[0][1]["project_id"] == "aming-claw"
        assert calls[0][1]["runtime_context_id"] == "mfrctx-live-host-continuation"
        assert calls[0][1]["task_id"] == "live-host-continuation-worker"
        assert calls[0][1]["session_token_ref"] == "wstok-live-before"
    else:
        assert calls[0][1]["project_id"] == "aming-claw"
        assert calls[0][1]["session_token_ref"] == "wstok-live-before"
        assert calls[0][1]["reason"] == "select the server-declared recovery"


def test_host_orchestration_explicit_precursor_precedes_recovery_catalog() -> None:
    guide, _receipt_body, _startup_body = _live_refreshing_host_startup_inputs()
    guide["session_token_rejoin_eligibility"] = {
        "eligible": True,
        "mode": "safe_ref_prestartup_reissue",
        "blockers": [],
    }
    guide["actionable_payloads"] = {
        "session_token_reissue_submission": {
            "mcp_tool": "runtime_context_session_token_reissue",
            "copy_safe_body": {"session_token_ref": "wstok-live-before"},
        }
    }
    calls = []

    result = orchestrate_runtime_context_host_startup(
        worker_guide=guide,
        tool_caller=lambda name, body: (
            calls.append((name, deepcopy(body)))
            or {"ok": False, "status": "rejected", "message": "test stop"}
        ),
        host_identity={
            "worker_session_id": "live-host-session",
            "host_startup_id": "live-host-startup",
            "head_commit": "a" * 40,
        },
    )

    assert result["ok"] is False
    assert [name for name, _body in calls] == [
        "runtime_context_session_token_rejoin"
    ]


@pytest.mark.parametrize(
    "failure_mode",
    [
        "blocked",
        "missing_packet",
        "conflicting_eligibility",
        "conflicting_packet_scope",
    ],
)
def test_host_orchestration_rejects_invalid_recovery_before_auth_call(
    failure_mode,
) -> None:
    guide, _expected_tool = _server_recovery_selected_host_guide(
        "pre_lineage_bootstrap_auth_only"
    )
    if failure_mode == "blocked":
        blocked = {"eligible": False, "mode": "blocked", "blockers": ["lease"]}
        guide["session_token_rejoin_eligibility"] = deepcopy(blocked)
        guide["actionable_payloads"]["session_token_rejoin_eligibility"] = (
            deepcopy(blocked)
        )
    elif failure_mode == "missing_packet":
        guide["actionable_payloads"].pop("session_token_rejoin_submission")
        guide["actionable_payloads"]["session_renewal_hints"].pop("rejoin")
    elif failure_mode == "conflicting_eligibility":
        guide["details"] = {
            "diagnostics": {
                "session_token_rejoin_eligibility": {
                    "eligible": True,
                    "mode": "safe_ref_prestartup_reissue",
                    "blockers": [],
                }
            }
        }
    else:
        conflicting = deepcopy(
            guide["actionable_payloads"]["session_token_rejoin_submission"]
        )
        conflicting["copy_safe_body"]["session_token_ref"] = "wstok-stale"
        guide["details"] = {
            "actionable_payloads": {
                "session_token_rejoin_submission": conflicting
            }
        }
    calls = []

    with pytest.raises(GuidedRuntimeDispatchError):
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=lambda name, body: calls.append((name, body)),
            host_identity={
                "worker_session_id": "live-host-session",
                "host_startup_id": "live-host-startup",
                "head_commit": "a" * 40,
            },
        )

    assert calls == []


@pytest.mark.parametrize(
    "precursor_tool",
    [
        "runtime_context_session_token_initial_join",
        "runtime_context_session_token_rejoin",
    ],
)
def test_host_orchestration_refreshes_live_packet_without_caller_reconstruction(
    precursor_tool,
) -> None:
    guide, receipt_body, startup_body = _live_refreshing_host_startup_inputs(
        precursor_tool
    )
    raw_session = "raw-live-host-session"
    raw_fence = "raw-live-host-fence"
    calls = []
    refresh_count = 0

    def call_tool(name, body):
        nonlocal refresh_count
        calls.append((name, body))
        if name == precursor_tool:
            return {
                "structuredContent": {
                    "ok": True,
                    "status": "session_token_rejoin_issued",
                    "worker_session_token_ref": "wstok-live-after",
                },
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "details": {
                                    "compatibility": {
                                        "worker_host_envelope": {
                                            "worker_session_token_ref": (
                                                "wstok-live-after"
                                            ),
                                            "env": {
                                                "AMING_WORKER_SESSION_TOKEN": raw_session,
                                                "AMING_WORKER_FENCE_TOKEN": raw_fence,
                                            },
                                        }
                                    }
                                }
                            }
                        ),
                    }
                ],
            }
        if name == "onboard_route_guide":
            refresh_count += 1
            if refresh_count == 1:
                return {
                    "structuredContent": {
                        "ok": True,
                        "status": "compact_response_size_exceeded",
                        "guide_capsule_ref": "gcap-live-receipt",
                    }
                }
            return {
                "structuredContent": {
                    "ok": True,
                    "details": {
                        "compatibility": {
                            "canonical_executable_action": {
                                "mcp_tool": "parallel_branch_startup",
                                "copy_safe_body": startup_body,
                            }
                        }
                    },
                }
            }
        if name == "onboard_route_guide_section_fetch":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "sections": {
                                    "action_input": {
                                        "canonical_executable_action": {
                                            "mcp_tool": (
                                                "runtime_context_read_receipt"
                                            ),
                                            "copy_safe_body": receipt_body,
                                        }
                                    }
                                }
                            }
                        ),
                    }
                ]
            }
        if name == "runtime_context_read_receipt":
            assert body["session_token"] == raw_session
            assert body["fence_token"] == raw_fence
            assert body["session_token_ref"] == "wstok-live-after"
            return {
                "ok": True,
                "status": "accepted",
                "read_receipt_hash": body["read_receipt_hash"],
                "read_receipt_event_id": "timeline:live-receipt",
            }
        assert name == "parallel_branch_startup"
        assert body["session_token"] == raw_session
        assert body["fence_token"] == raw_fence
        assert body["read_receipt_event_id"] == "timeline:live-receipt"
        assert body["owned_files"] == [
            "src/reminders.js",
            "tests/reminders.test.mjs",
        ]
        return {"ok": True, "status": "startup_recorded"}

    result = orchestrate_runtime_context_host_startup(
        worker_guide={
            "content": [{"type": "text", "text": json.dumps(guide)}]
        },
        tool_caller=call_tool,
        host_identity={
            "worker_session_id": "live-host-session",
            "host_startup_id": "live-host-startup",
            "head_commit": "a" * 40,
            "launch_text_hash": "sha256:" + "a" * 64,
        },
    )

    assert result["ok"] is True
    assert result["guide_refreshed_after_each_transition"] is True
    assert [name for name, _body in calls] == [
        precursor_tool,
        "onboard_route_guide",
        "onboard_route_guide_section_fetch",
        "runtime_context_read_receipt",
        "onboard_route_guide",
        "parallel_branch_startup",
    ]
    assert raw_session not in json.dumps(result, sort_keys=True)
    assert raw_fence not in json.dumps(result, sort_keys=True)
    assert all(raw_session not in json.dumps(body) for _name, body in calls)
    assert all(raw_fence not in json.dumps(body) for _name, body in calls)


@pytest.mark.parametrize(
    "invalid_owned_files",
    [None, [], "src/reminders.js", ["<owned file>"], ["same.js", "same.js"]],
)
def test_host_orchestration_rejects_invalid_refreshed_owned_files_before_startup(
    invalid_owned_files,
) -> None:
    guide, receipt_body, startup_body = _live_refreshing_host_startup_inputs()
    if invalid_owned_files is None:
        startup_body.pop("owned_files")
    else:
        startup_body["owned_files"] = invalid_owned_files
    calls = []
    refresh_count = 0

    def call_tool(name, body):
        nonlocal refresh_count
        calls.append(name)
        if name == "runtime_context_session_token_rejoin":
            return {
                "ok": True,
                "worker_session_token_ref": "wstok-live-after",
                "host_envelope": {
                    "session_token_ref": "wstok-live-after",
                    "env": {
                        "AMING_WORKER_SESSION_TOKEN": "raw-live-session",
                        "AMING_WORKER_FENCE_TOKEN": "raw-live-fence",
                    },
                },
            }
        if name == "onboard_route_guide":
            refresh_count += 1
            action = (
                {
                    "mcp_tool": "runtime_context_read_receipt",
                    "copy_safe_body": receipt_body,
                }
                if refresh_count == 1
                else {
                    "mcp_tool": "parallel_branch_startup",
                    "copy_safe_body": startup_body,
                }
            )
            return {"structuredContent": {"canonical_executable_action": action}}
        if name == "runtime_context_read_receipt":
            return {
                "ok": True,
                "read_receipt_hash": body["read_receipt_hash"],
                "read_receipt_event_id": "timeline:invalid-owned-files",
            }
        pytest.fail("invalid owned_files reached the startup tool")

    with pytest.raises(GuidedRuntimeDispatchError):
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=call_tool,
            host_identity={
                "worker_session_id": "live-host-session",
                "host_startup_id": "live-host-startup",
                "head_commit": "a" * 40,
                "launch_text_hash": "sha256:" + "a" * 64,
            },
        )

    assert calls == [
        "runtime_context_session_token_rejoin",
        "onboard_route_guide",
        "runtime_context_read_receipt",
        "onboard_route_guide",
    ]


@pytest.mark.parametrize("failure_mode", ["wrong_scope", "conflicting_alias"])
def test_host_orchestration_rejects_invalid_refreshed_packet_before_receipt(
    failure_mode,
) -> None:
    guide, receipt_body, _startup_body = _live_refreshing_host_startup_inputs()
    bad_body = deepcopy(receipt_body)
    bad_body["task_id"] = "wrong-worker"
    receipt_action = {
        "mcp_tool": "runtime_context_read_receipt",
        "copy_safe_body": bad_body if failure_mode == "wrong_scope" else receipt_body,
    }
    calls = []

    def call_tool(name, body):
        calls.append(name)
        if name == "runtime_context_session_token_rejoin":
            return {
                "ok": True,
                "worker_session_token_ref": "wstok-live-after",
                "host_envelope": {
                    "session_token_ref": "wstok-live-after",
                    "env": {
                        "AMING_WORKER_SESSION_TOKEN": "raw-live-session",
                        "AMING_WORKER_FENCE_TOKEN": "raw-live-fence",
                    },
                },
            }
        if name == "onboard_route_guide":
            response = {"structuredContent": {"canonical_executable_action": receipt_action}}
            if failure_mode == "conflicting_alias":
                response["content"] = [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "canonical_executable_action": {
                                    "mcp_tool": "runtime_context_read_receipt",
                                    "copy_safe_body": bad_body,
                                }
                            }
                        ),
                    }
                ]
            return response
        pytest.fail("invalid continuation packet reached a downstream write")

    with pytest.raises(GuidedRuntimeDispatchError):
        orchestrate_runtime_context_host_startup(
            worker_guide=guide,
            tool_caller=call_tool,
            host_identity={
                "worker_session_id": "live-host-session",
                "host_startup_id": "live-host-startup",
                "head_commit": "a" * 40,
            },
        )

    assert calls == [
        "runtime_context_session_token_rejoin",
        "onboard_route_guide",
    ]


def _graph_continuation_inputs():
    route = {
        "route_id": "route-graph-continuation",
        "route_context_hash": "sha256:" + "1" * 64,
        "prompt_contract_id": "prompt-graph-continuation",
        "prompt_contract_hash": "sha256:" + "2" * 64,
        "route_token_ref": "rtok-graph-continuation",
        "visible_injection_manifest_hash": "sha256:" + "3" * 64,
    }
    body = {
        "project_id": "aming-claw",
        "backlog_id": "AC-GRAPH-CONTINUATION",
        "runtime_context_id": "mfrctx-graph-continuation",
        "task_id": "graph-continuation-worker",
        "parent_task_id": "cex-graph-continuation",
        "target_project_root": "/tmp/graph-continuation",
        "project_root": "/tmp/graph-continuation",
        "repo_root": "/tmp/graph-continuation",
        "worker_role": "mf_sub",
        "query_source": "mf_subagent",
        "query_purpose": "subagent_context_build",
        "tool": "function_index",
        "args": {"query": "<exact source symbol name>"},
        "session_token_ref": "wstok-graph-rotated",
        "route_identity": route,
    }
    guide = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"corrected_request_shapes": {"graph_query_body": body}}
                ),
            }
        ]
    }
    auth = {
        "structuredContent": {
            "worker_session_token_ref": "wstok-graph-rotated",
            "worker_host_envelope": {
                "worker_session_token_ref": "wstok-graph-rotated",
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": "raw-session-graph",
                    "AMING_WORKER_FENCE_TOKEN": "raw-fence-graph",
                },
            },
        }
    }
    scope = {
        key: value
        for key, value in body.items()
        if key
        in {
            "project_id",
            "backlog_id",
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "target_project_root",
            "project_root",
            "repo_root",
            "query_source",
            "query_purpose",
        }
    }
    scope["route_identity"] = route
    return guide, auth, scope


def test_mcp_application_mapping_blocks_reads_only_declared_compatibility_path() -> None:
    response = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "details": {
                            "compatibility": {
                                "corrected_request_shapes": {
                                    "graph_query_body": {"project_id": "aming-claw"}
                                }
                            }
                        },
                        "unrelated": {
                            "graph_query_body": {"project_id": "wrong-project"}
                        },
                    }
                ),
            }
        ]
    }

    blocks = mcp_application_mapping_blocks(
        response,
        paths=(
            (
                "details",
                "compatibility",
                "corrected_request_shapes",
                "graph_query_body",
            ),
        ),
    )

    assert blocks == [{"project_id": "aming-claw"}]


def test_graph_continuation_preserves_scope_route_and_scrubs_auth() -> None:
    guide, auth, scope = _graph_continuation_inputs()
    live_bodies = []

    def call_tool(name, body):
        assert name == "graph_query"
        live_bodies.append(body)
        assert body["route_identity"] == scope["route_identity"]
        assert body["backlog_id"] == scope["backlog_id"]
        return {
            "structuredContent": {
                "ok": True,
                "status": "passed",
                "trace_id": "gtrace-{}".format(len(live_bodies)),
            }
        }

    queries = [
        {"tool": tool, "args": {"query": "orchestrate_runtime_context_host_startup"}}
        for tool in ("function_index", "function_callers", "function_callees")
    ]
    result = orchestrate_runtime_context_graph_continuation(
        worker_guide=guide,
        host_auth_response=auth,
        tool_caller=call_tool,
        expected_scope=scope,
        queries=queries,
    )

    assert result["graph_trace_ids"] == ["gtrace-1", "gtrace-2", "gtrace-3"]
    assert [item["tool"] for item in result["queries"]] == [
        "function_index",
        "function_callers",
        "function_callees",
    ]
    assert result["session_token_ref"] == "wstok-graph-rotated"
    serialized = json.dumps(result, sort_keys=True)
    assert "raw-session-graph" not in serialized
    assert "raw-fence-graph" not in serialized
    assert all("raw-session-graph" not in json.dumps(body) for body in live_bodies)
    assert all("raw-fence-graph" not in json.dumps(body) for body in live_bodies)


def test_graph_continuation_conflicting_scope_is_zero_call() -> None:
    guide, auth, scope = _graph_continuation_inputs()
    scope["task_id"] = "conflicting-task"
    calls = []

    with pytest.raises(GuidedRuntimeDispatchError, match="task_id"):
        orchestrate_runtime_context_graph_continuation(
            worker_guide=guide,
            host_auth_response=auth,
            tool_caller=lambda name, body: calls.append((name, body)),
            expected_scope=scope,
            queries=[{"tool": "function_index", "args": {"query": "exact_symbol"}}],
        )

    assert calls == []


def test_graph_continuation_missing_expected_route_is_zero_call() -> None:
    guide, auth, scope = _graph_continuation_inputs()
    scope["route_identity"].pop("route_token_ref")
    calls = []

    with pytest.raises(GuidedRuntimeDispatchError, match="route_token_ref"):
        orchestrate_runtime_context_graph_continuation(
            worker_guide=guide,
            host_auth_response=auth,
            tool_caller=lambda name, body: calls.append((name, body)),
            expected_scope=scope,
            queries=[{"tool": "function_index", "args": {"query": "exact_symbol"}}],
        )

    assert calls == []


def test_graph_continuation_ambiguous_guide_is_zero_call() -> None:
    guide, auth, scope = _graph_continuation_inputs()
    application = json.loads(guide["content"][0]["text"])
    conflicting = deepcopy(application["corrected_request_shapes"]["graph_query_body"])
    conflicting["task_id"] = "other-task"
    application["details"] = {
        "corrected_request_shapes": {"graph_query_body": conflicting}
    }
    guide["content"][0]["text"] = json.dumps(application)
    calls = []

    with pytest.raises(GuidedRuntimeDispatchError, match="ambiguous"):
        orchestrate_runtime_context_graph_continuation(
            worker_guide=guide,
            host_auth_response=auth,
            tool_caller=lambda name, body: calls.append((name, body)),
            expected_scope=scope,
            queries=[{"tool": "function_index", "args": {"query": "exact_symbol"}}],
        )

    assert calls == []


def _implementation_continuation_inputs():
    runtime_context_id = "mfrctx-implementation-continuation"
    session_token_ref = "wstok-implementation-rotated"
    route_token_ref = "rtok-implementation-continuation"
    body = {
        "project_id": "aming-claw",
        "runtime_context_id": runtime_context_id,
        "task_id": "implementation-continuation-worker",
        "parent_task_id": "cex-implementation-continuation",
        "lane_id": "implementation-continuation-worker",
        "worker_role": "mf_sub",
        "worker_id": "implementation-continuation-worker",
        "worker_slot_id": "implementation-continuation-worker",
        "target_project_root": "/tmp/implementation-continuation",
        "session_token": "<read from worker env>",
        "session_token_ref": session_token_ref,
        "fence_token": "<read from worker env>",
        "session_token_env": "AMING_WORKER_SESSION_TOKEN",
        "fence_token_env": "AMING_WORKER_FENCE_TOKEN",
        "changed_files": ["<cumulative changed file>"],
        "tests": [{"command": "<test command>", "status": "passed"}],
        "test_results": {
            "status": "passed",
            "commands": [{"command": "<test command>", "status": "passed"}],
        },
        "graph_trace_ids": ["<worker graph trace>"],
        "payload": {
            "graph_trace_ids": ["<worker graph trace>"],
            "worker_session_lifecycle_policy": {
                "fence_token": "<must not persist raw auth>"
            },
        },
        "route_token_ref": route_token_ref,
    }
    guide = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "details": {
                            "actionable_payloads": {
                                "implementation_evidence_facade_payload_skeleton": {
                                    "copy_safe_body": body
                                }
                            }
                        }
                    }
                ),
            }
        ]
    }
    auth = {
        "structuredContent": {
            "worker_session_token_ref": session_token_ref,
            "worker_host_envelope": {
                "worker_session_token_ref": session_token_ref,
                "env": {
                    "AMING_WORKER_SESSION_TOKEN": "raw-implementation-session",
                    "AMING_WORKER_FENCE_TOKEN": "raw-implementation-fence",
                },
            },
        }
    }
    binding = {
        "backlog_id": "AC-IMPLEMENTATION-CONTINUATION",
        "definition_hash": "sha256:" + "1" * 64,
        "instruction_bundle_hash": "sha256:" + "2" * 64,
        "execution_state_revision": 7,
        "runtime_guide_hash": "sha256:" + "3" * 64,
        "stage_id": "worker_implementation",
        "line_id": "worker_implementation",
        "evidence_kind": "implementation",
        "line_instance_id": "runtime_context:{}".format(runtime_context_id),
    }
    writer = {
        "copy_payload": binding,
        "hash_alignment": {
            "required_writer_runtime_guide_hash": binding["runtime_guide_hash"]
        },
    }
    current = {
        "structuredContent": {
            "ok": True,
            "runtime_guide": {"writer_role_safe_copy_payload": writer},
            "next_legal_action": {"writer_role_safe_copy_payload": writer},
        }
    }
    scope = {
        "project_id": "aming-claw",
        "backlog_id": binding["backlog_id"],
        "contract_execution_id": "cex-implementation-continuation",
        "runtime_context_id": runtime_context_id,
        "task_id": body["task_id"],
        "parent_task_id": body["parent_task_id"],
        "target_project_root": body["target_project_root"],
        "route_token_ref": route_token_ref,
    }
    evidence = {
        "changed_files": ["agent/cli_agent_service/guided_runtime.py"],
        "tests": [{"command": "python -m pytest focused.py", "status": "passed"}],
        "test_results": {"status": "passed", "passed": True},
        "graph_trace_ids": ["gqt-implementation-continuation"],
        "commit_sha": "a" * 40,
        "head_commit": "a" * 40,
        "clean_worktree": True,
        "dirty_files": [],
    }
    return guide, auth, current, scope, evidence, binding


def test_implementation_continuation_uses_current_atomic_writer_binding() -> None:
    guide, auth, current, scope, evidence, binding = (
        _implementation_continuation_inputs()
    )
    calls = []
    live_bodies = []

    def call_tool(name, body):
        calls.append(name)
        live_bodies.append(body)
        if name == "runtime_context_current":
            assert body == {
                "project_id": scope["project_id"],
                "runtime_context_id": scope["runtime_context_id"],
                "parent_task_id": scope["parent_task_id"],
                "target_project_root": scope["target_project_root"],
                "session_token": "raw-implementation-session",
                "fence_token": "raw-implementation-fence",
                "session_token_ref": "wstok-implementation-rotated",
                "view": "all",
            }
            return current
        assert name == "runtime_context_implementation_evidence"
        assert {key: body[key] for key in binding} == binding
        assert body["session_token_ref"] == "wstok-implementation-rotated"
        assert body["changed_files"] == evidence["changed_files"]
        assert body["payload"]["graph_trace_ids"] == evidence["graph_trace_ids"]
        assert "worker_session_lifecycle_policy" not in body["payload"]
        return {
            "ok": True,
            "status": "passed",
            "implementation_event_ref": "timeline:implementation-continuation",
        }

    result = orchestrate_runtime_context_implementation_continuation(
        worker_guide=guide,
        host_auth_response=auth,
        tool_caller=call_tool,
        expected_scope=scope,
        implementation_evidence=evidence,
    )

    assert calls == [
        "runtime_context_current",
        "runtime_context_implementation_evidence",
    ]
    assert result["atomic_writer_binding_used"] is True
    assert result["implementation_event_ref"] == (
        "timeline:implementation-continuation"
    )
    serialized = json.dumps(result, sort_keys=True)
    assert "raw-implementation-session" not in serialized
    assert "raw-implementation-fence" not in serialized
    assert all(
        "raw-implementation-session" not in json.dumps(body)
        and "raw-implementation-fence" not in json.dumps(body)
        for body in live_bodies
    )


@pytest.mark.parametrize(
    ("failure_kind", "message"),
    [
        ("missing", "missing definition_hash"),
        ("wrong", "line_instance_id"),
        ("stale", "stale"),
    ],
)
def test_implementation_continuation_invalid_binding_is_zero_write(
    failure_kind, message
) -> None:
    guide, auth, current, scope, evidence, _binding = (
        _implementation_continuation_inputs()
    )
    writer = current["structuredContent"]["runtime_guide"][
        "writer_role_safe_copy_payload"
    ]
    if failure_kind == "missing":
        writer["copy_payload"].pop("definition_hash")
    elif failure_kind == "wrong":
        writer["copy_payload"]["line_instance_id"] = "runtime_context:wrong"
    else:
        writer["hash_alignment"]["required_writer_runtime_guide_hash"] = (
            "sha256:" + "9" * 64
        )
    current["structuredContent"]["next_legal_action"][
        "writer_role_safe_copy_payload"
    ] = writer
    calls = []

    def call_tool(name, body):
        calls.append((name, body))
        assert name == "runtime_context_current"
        return current

    with pytest.raises(GuidedRuntimeDispatchError, match=message):
        orchestrate_runtime_context_implementation_continuation(
            worker_guide=guide,
            host_auth_response=auth,
            tool_caller=call_tool,
            expected_scope=scope,
            implementation_evidence=evidence,
        )

    assert [name for name, _body in calls] == ["runtime_context_current"]


def test_implementation_continuation_rejects_caller_binding_override_zero_call() -> None:
    guide, auth, _current, scope, evidence, binding = (
        _implementation_continuation_inputs()
    )
    evidence["runtime_guide_hash"] = binding["runtime_guide_hash"]
    calls = []

    with pytest.raises(GuidedRuntimeDispatchError, match="caller-owned authority"):
        orchestrate_runtime_context_implementation_continuation(
            worker_guide=guide,
            host_auth_response=auth,
            tool_caller=lambda name, body: calls.append((name, body)),
            expected_scope=scope,
            implementation_evidence=evidence,
        )

    assert calls == []
