"""Observer runtime launcher contracts.

The observer launcher is intentionally thin: it converts route/backlog context
into a provider-neutral AI invocation request. ServiceManager or future manager
HTTP endpoints can call the same functions without depending on click.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from ai_invocation import (
        AIInvocationRequest,
        AIInvocationResult,
        RoutePromptContract,
        build_codex_exec_command,
        invoke_ai,
    )
    from governance.mf_subagent_contract import (
        MfSubagentContractError,
        build_mf_subagent_input,
        validate_mf_subagent_dispatch_gate,
    )
    from governance import batch_jobs
    from governance.parallel_branch_runtime import (
        build_registered_host_adapter_spawn_identity,
        branch_strategy_from_runtime_context,
        plan_branch_runtime_context,
        runtime_context_id_for_branch_context,
        runtime_context_session_token_ref,
    )
except ImportError:  # pragma: no cover - package import path
    from agent.ai_invocation import (
        BACKEND_CLAUDE_CLI,
        BACKEND_CODEX_CLI,
        BACKEND_DOCKER_LIVE_AI,
        AIInvocationRequest,
        AIInvocationResult,
        RoutePromptContract,
        build_codex_exec_command,
        invoke_ai,
    )
    from agent.governance.mf_subagent_contract import (
        MfSubagentContractError,
        build_mf_subagent_input,
        validate_mf_subagent_dispatch_gate,
    )
    from agent.governance import batch_jobs
    from agent.governance.parallel_branch_runtime import (
        build_registered_host_adapter_spawn_identity,
        branch_strategy_from_runtime_context,
        plan_branch_runtime_context,
        runtime_context_id_for_branch_context,
        runtime_context_session_token_ref,
    )
else:  # pragma: no cover - direct module import path
    from ai_invocation import BACKEND_CLAUDE_CLI, BACKEND_CODEX_CLI, BACKEND_DOCKER_LIVE_AI


OBSERVER_RUN_SCHEMA_VERSION = "observer_run.v1"
OBSERVER_POLL_SCHEMA_VERSION = "observer_poll.v1"
OBSERVER_POLL_LOOP_SCHEMA_VERSION = "observer_poll_loop.v1"
OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION = "observer_poll_timeline_payload.v1"
DOGFOOD_OBSERVER_PLAN_SCHEMA_VERSION = "observer_dogfood_plan.v1"
OBSERVER_RUNTIME_TEXT_SCHEMA_VERSION = "observer_runtime_text_context.v1"
OBSERVER_RUNTIME_TEXT_SERVICE_SCHEMA_VERSION = "observer_runtime_text_service.v1"
OBSERVER_WORKER_LAUNCH_PACK_SCHEMA_VERSION = "observer_worker_launch_pack.v1"
OBSERVER_EXECUTABLE_WORKER_LAUNCH_SCHEMA_VERSION = "observer_executable_worker_launch.v1"
OBSERVER_EXECUTABLE_HANDOFF_PACKET_SCHEMA_VERSION = (
    "observer_runtime_text.executable_handoff_packet.v1"
)
OBSERVER_RUNTIME_TEXT_WORKER_ENVELOPE_CLAIM_SCHEMA_VERSION = (
    "observer_runtime_text.worker_envelope_claim.v1"
)
OBSERVER_RUNTIME_TEXT_NEXT_LEGAL_ACTION_SCHEMA_VERSION = (
    "observer_runtime_text.next_legal_action.v1"
)
BOUNDED_WORKER_NO_PROGRESS_NEXT_ACTION_SCHEMA_VERSION = (
    "bounded_worker_no_progress_next_action.v1"
)
OBSERVER_EXECUTABLE_LAUNCH_ENV_POLICY_SCHEMA_VERSION = (
    "observer_executable_worker_launch.env_policy.v1"
)
ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION = "observer_one_hop_execution_gate.v1"
EXECUTE_BACKLOG_ROW_COMMAND_TYPE = "execute_backlog_row"
EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS = (
    "backlog_id",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_hash",
)
ONE_HOP_REQUIRED_BACKENDS = {
    BACKEND_CODEX_CLI,
    BACKEND_CLAUDE_CLI,
    BACKEND_DOCKER_LIVE_AI,
}
BACKEND_CODEX_APP_SUBAGENT = "codex_app_subagent"
CODEX_CLI_WORKER_BACKEND_ALIASES = {
    BACKEND_CODEX_CLI,
    "codex_cli_exec",
}
CODEX_APP_SUBAGENT_WORKER_BACKEND_ALIASES = {
    BACKEND_CODEX_APP_SUBAGENT,
    "codex_app",
    "codex_app_worker",
    "codex_app_subagent_host_adapter",
}
WORKER_LAUNCH_BACKENDS = ONE_HOP_REQUIRED_BACKENDS | (
    CODEX_CLI_WORKER_BACKEND_ALIASES - {BACKEND_CODEX_CLI}
) | CODEX_APP_SUBAGENT_WORKER_BACKEND_ALIASES
OBSERVER_POLL_TIMELINE_ROUTE_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_hash",
)
OBSERVER_POLL_TIMELINE_FLAG_FIELDS = (
    "execute",
    "calls_models",
    "service_manager_required",
    "executor_worker_required",
    "uses_task_create",
)
RUNTIME_TEXT_DEFAULT_TOPOLOGY = "mf_parallel.v1"
RUNTIME_TEXT_REQUIRED_EVIDENCE = (
    "parent_route_lineage",
    "dispatch_graph_first_obligation",
    "finish_worker_graph_trace_evidence",
    "branch_runtime_evidence",
    "service_dispatch_evidence",
    "startup_echo",
    "first_progress_evidence",
    "finish_gate",
)
WORKER_LAUNCH_PACK_REQUIRED_FIELDS = (
    "schema_version",
    "project_id",
    "backlog_id",
    "task_id",
    "runtime_context_id",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "worker_role",
    "branch",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "fence_token_hash",
    "fence_token_env",
    "owned_files",
    "merge_queue_id",
    "graph_query_schema_trace_id",
    "context_pack_refs",
    "context_pack_status",
    "runtime_context_worker_envelope_claim",
    "local_runtime_context_bridge",
    "runtime_context_entrypoints",
    "cli_runtime_requirements",
    "worker_guide_ref",
    "worker_guide_hash",
    "worker_guide_status",
    "allowed_actions",
    "blocked_actions",
    "next_legal_action",
    "startup_preflight",
    "test_environment_preflight",
    "required_evidence",
    "transcript_refs",
    "transcript_digests",
)
EXECUTABLE_WORKER_LAUNCH_REQUIRED_FIELDS = (
    "project_id",
    "backlog_id",
    "observer_command_id",
    "task_id",
    "parent_task_id",
    "runtime_context_id",
    "worker_role",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_hash",
    "route_token_ref",
    "worktree_path",
    "branch",
    "base_commit",
    "target_head_commit",
    "fence_token_hash",
    "fence_token_env",
    "merge_queue_id",
    "owned_files",
    "launch_text_hash",
    "session_token_env",
)
EXECUTABLE_WORKER_LAUNCH_PRESERVE_HOST_ENV = (
    "PATH",
    "HOME",
    "SHELL",
    "TMPDIR",
)
WORKER_LAUNCH_PACK_ALLOWED_ACTIONS = (
    "submit_mf_subagent_read_receipt",
    "record_mf_subagent_startup",
    "run_worker_graph_query",
    "patch_owned_files",
    "run_focused_tests",
    "record_implementation_evidence",
    "task_timeline_append",
    "record_finish_time_worker_attestation",
    "record_finish_gate",
    "report_review_ready",
    "report_waiting_merge",
)
WORKER_LAUNCH_PACK_BLOCKED_ACTIONS = (
    "author_worker_evidence_as_observer",
    "bypass_timeline_gate",
    "surrogate_startup",
    "raw_token_exfiltration",
    "git_commit_before_finish_gate",
    "emit_git_commit_directive_before_finish_gate",
    "merge",
    "push",
    "activate_graph",
    "release_gate",
    "create_task",
    "delete_worktree",
    "modify_merge_queue",
    "close_backlog_without_close_ready",
)
WORKER_LAUNCH_PACK_OBSERVER_ONLY_NEXT_ACTIONS = {
    "record_work_mode_transition",
    "dispatch_bounded_worker",
    "observer_dispatch_bounded_worker",
    "route_action_precheck",
    "merge",
    "close",
    "close_backlog",
    "modify_merge_queue",
}


def _canonical_worker_launch_backend(backend_mode: str) -> str:
    normalized = str(backend_mode or "").strip()
    if normalized in CODEX_CLI_WORKER_BACKEND_ALIASES:
        return BACKEND_CODEX_CLI
    if normalized in CODEX_APP_SUBAGENT_WORKER_BACKEND_ALIASES:
        return BACKEND_CODEX_APP_SUBAGENT
    return normalized
RUNTIME_TEXT_BRANCH_RUNTIME_REF_MARKERS = (
    "/parallel-branches/allocate",
    "parallel-branches/allocate",
    "/parallel-branches/runtime-contexts/",
    "parallel-branches/runtime-contexts/",
    "/runtime-contract",
    "upsert_branch_context",
)
RUNTIME_TEXT_REQUIRED_LANES = (
    "observer_coordinator",
    "bounded_implementation_worker",
    "observer_review_gate",
)
RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION = "runtime_context.current.v1"
RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION = "runtime_context.gate_inputs.v1"
RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION = "runtime_context.worker_view.v1"
RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION = "runtime_context.close_gate_view.v1"
RUNTIME_TEXT_RUNTIME_CONTEXT_PROJECTION_CONTRACT = (
    "Worker A integration point: consume runtime_context.current.v1, "
    "runtime_context.gate_inputs.v1, runtime_context.worker_view.v1, and "
    "runtime_context.close_gate_view.v1 when supplied by the Runtime Context "
    "Service; otherwise use this module's local compatibility projection."
)
RUNTIME_TEXT_STARTUP_PROJECTION_FIELDS = (
    "runtime_context_id",
    "observer_command_id",
    "task_id",
    "parent_task_id",
    "worker_role",
    "fence_token",
    "worktree_path",
    "branch_ref",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "graph_query_identity",
)
RUNTIME_TEXT_FINISH_PROJECTION_FIELDS = (
    "runtime_context_id",
    "observer_command_id",
    "task_id",
    "parent_task_id",
    "worker_role",
    "fence_token",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "owned_files",
)
RUNTIME_TEXT_PROJECTION_FIELD_SOURCES = {
    "runtime_context_id": (
        "runtime_context.current.v1.worker_view.runtime_context_id"
    ),
    "observer_command_id": (
        "runtime_context.gate_inputs.v1.observer_command_id"
    ),
    "task_id": "runtime_context.current.v1.worker_view.task_id",
    "parent_task_id": "runtime_context.current.v1.worker_view.parent_task_id",
    "worker_role": "runtime_context.worker_view.v1.worker_role",
    "fence_token": "runtime_context.worker_view.v1.fence_token",
    "worktree_path": "runtime_context.worker_view.v1.worktree_path",
    "branch_ref": "runtime_context.worker_view.v1.branch_ref",
    "base_commit": "runtime_context.worker_view.v1.base_commit",
    "target_head_commit": "runtime_context.worker_view.v1.target_head_commit",
    "merge_queue_id": "runtime_context.worker_view.v1.merge_queue_id",
    "owned_files": "runtime_context.worker_view.v1.target_files",
    "route_context_hash": "runtime_context.gate_inputs.v1.route_context_hash",
    "prompt_contract_id": "runtime_context.gate_inputs.v1.prompt_contract_id",
    "prompt_contract_hash": "runtime_context.gate_inputs.v1.prompt_contract_hash",
    "graph_query_identity": "runtime_context.worker_view.v1.graph_query_identity",
}


@dataclass
class ObserverRunRequest:
    project_id: str
    backlog_id: str
    route: RoutePromptContract
    provider: str = "openai"
    model: str = ""
    backend_mode: str = "codex_cli"
    workspace: str = ""
    prompt: str = ""
    timeout_sec: int = 120
    early_progress_timeout_sec: float = 20.0
    dispatch_gate: Mapping[str, Any] = field(default_factory=dict)
    main_worktree: str = ""
    heartbeat_callback: Callable[[], Mapping[str, Any]] | None = None
    heartbeat_interval_sec: float = 0.0
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_route_token(
        cls,
        *,
        project_id: str,
        backlog_id: str,
        route_token: Mapping[str, Any],
        provider: str = "openai",
        model: str = "",
        backend_mode: str = "codex_cli",
        workspace: str = "",
        prompt: str = "",
        timeout_sec: int = 120,
        early_progress_timeout_sec: float = 20.0,
    ) -> "ObserverRunRequest":
        return cls(
            project_id=project_id,
            backlog_id=backlog_id,
            route=RoutePromptContract.from_mapping({"route_token": route_token}),
            provider=provider,
            model=model,
            backend_mode=backend_mode,
            workspace=workspace,
            prompt=prompt,
            timeout_sec=timeout_sec,
            early_progress_timeout_sec=early_progress_timeout_sec,
        )


@dataclass
class DogfoodObserverPlanRequest:
    project_id: str
    backlog_id: str
    route: RoutePromptContract
    governance_project_id: str = ""
    target_project_id: str = ""
    target_project_root: str = ""
    allocation_owner: str = ""
    provider: str = "openai"
    model: str = ""
    backend_mode: str = "codex_cli"
    main_worktree: str = ""
    workspace_root: str = ""
    owned_files: tuple[str, ...] = ()
    task_id: str = ""
    worker_id: str = ""
    attempt: int = 1
    worktree_root: str = ".worktrees"
    branch_prefix: str = "dogfood"
    merge_queue_id: str = ""
    fence_token: str = ""
    graph_trace_ids: tuple[str, ...] = ()
    branch_runtime_registration_ref: str = ""
    branch_runtime_evidence: Mapping[str, Any] = field(default_factory=dict)
    runtime_context_id: str = ""
    base_commit: str = ""
    target_head_commit: str = ""
    prompt: str = ""
    timeout_sec: int = 120
    early_progress_timeout_sec: float = 20.0
    route_id: str = ""
    precheck_run_id: str = ""
    visible_injection_manifest_hash: str = ""


@dataclass
class ObserverRuntimeTextPrepareRequest:
    project_id: str
    backlog_id: str
    route: RoutePromptContract
    governance_project_id: str = ""
    target_project_id: str = ""
    target_project_root: str = ""
    allocation_owner: str = ""
    main_worktree: str = ""
    workspace_root: str = ""
    owned_files: tuple[str, ...] = ()
    observer_command_id: str = ""
    task_id: str = ""
    parent_task_id: str = ""
    worker_id: str = ""
    attempt: int = 1
    worktree_root: str = ".worktrees"
    branch_prefix: str = "runtime-text"
    merge_queue_id: str = ""
    fence_token: str = ""
    graph_trace_ids: tuple[str, ...] = ()
    runtime_context_projection: Mapping[str, Any] = field(default_factory=dict)
    branch_runtime_registration_ref: str = ""
    branch_runtime_evidence: Mapping[str, Any] = field(default_factory=dict)
    runtime_context_id: str = ""
    base_commit: str = ""
    target_head_commit: str = ""
    prompt: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()
    route_id: str = ""
    precheck_run_id: str = ""
    visible_injection_manifest_hash: str = ""
    backend_mode: str = ""
    startup_source: str = ""
    host_adapter_agent_id: str = ""
    actual_host_worker_id: str = ""
    host_startup_id: str = ""
    host_session_id: str = ""
    session_token_surrogate: str = ""
    parent_route_identity: Mapping[str, Any] = field(default_factory=dict)
    graph_query_schema_trace_id: str = ""
    context_pack_refs: tuple[str, ...] = ()
    context_pack_status: str = ""
    context_pack_resolution: Mapping[str, Any] = field(default_factory=dict)
    worker_guide_ref: str = ""
    worker_guide_hash: str = ""
    worker_guide_status: str = ""
    worker_next_legal_action: str = ""
    startup_prerequisites: Mapping[str, Any] = field(default_factory=dict)
    read_receipt_hash: str = ""
    read_receipt_event_id: str = ""
    read_receipt: Mapping[str, Any] = field(default_factory=dict)
    transcript_refs: tuple[str, ...] = ()
    transcript_digests: tuple[str, ...] = ()
    selected_topology: str = RUNTIME_TEXT_DEFAULT_TOPOLOGY
    recommended_topology: str = RUNTIME_TEXT_DEFAULT_TOPOLOGY


@dataclass
class ObserverPollRequest:
    project_id: str
    command: Mapping[str, Any] | None = None
    observer_session_id: str = ""
    provider: str = "openai"
    model: str = ""
    backend_mode: str = "codex_cli"
    workspace: str = ""
    prompt: str = ""
    timeout_sec: int = 120
    early_progress_timeout_sec: float = 20.0
    dispatch_gate: Mapping[str, Any] = field(default_factory=dict)
    main_worktree: str = ""
    heartbeat_callback: Callable[[], Mapping[str, Any]] | None = None
    heartbeat_interval_sec: float = 0.0


@dataclass
class ObserverPollLoopConfig:
    watch: bool = False
    max_commands: int = 0
    idle_timeout_sec: float = 0.0
    poll_interval_sec: float = 5.0


def build_observer_poll_loop_metadata(config: ObserverPollLoopConfig) -> dict[str, Any]:
    """Normalize bounded observer poll loop settings for CLI/API evidence."""

    max_commands = max(0, int(config.max_commands or 0))
    idle_timeout_sec = max(0.0, float(config.idle_timeout_sec or 0.0))
    poll_interval_sec = max(0.0, float(config.poll_interval_sec or 0.0))
    effective_max_commands = max_commands if config.watch else 1
    return {
        "schema_version": OBSERVER_POLL_LOOP_SCHEMA_VERSION,
        "watch": bool(config.watch),
        "once": not bool(config.watch),
        "max_commands": max_commands,
        "effective_max_commands": effective_max_commands,
        "idle_timeout_sec": idle_timeout_sec,
        "poll_interval_sec": poll_interval_sec,
        "heartbeat_count": 0,
        "claim_attempts": 0,
        "empty_polls": 0,
        "processed_count": 0,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
    }


def validate_observer_run_request(request: ObserverRunRequest) -> list[str]:
    missing: list[str] = []
    if not request.project_id:
        missing.append("project_id")
    if not request.backlog_id:
        missing.append("backlog_id")
    if not request.route.route_context_hash:
        missing.append("route_context_hash")
    if not request.route.prompt_contract_id:
        missing.append("prompt_contract_id")
    if not request.provider:
        missing.append("provider")
    if not request.backend_mode:
        missing.append("backend_mode")
    return missing


def _command_payload(command: Mapping[str, Any] | None) -> dict[str, Any]:
    if not command:
        return {}
    payload = command.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    payload_json = command.get("payload_json")
    if isinstance(payload_json, str):
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _missing_execute_backlog_payload_fields(payload: Mapping[str, Any]) -> list[str]:
    return [
        field
        for field in EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS
        if not str(payload.get(field) or "").strip()
    ]


def _route_from_command_payload(payload: Mapping[str, Any]) -> RoutePromptContract:
    return RoutePromptContract(
        route_context_hash=str(payload.get("route_context_hash") or ""),
        prompt_contract_id=str(payload.get("prompt_contract_id") or ""),
        prompt_contract_hash=str(payload.get("prompt_contract_hash") or ""),
        route_token_ref=str(payload.get("route_token_ref") or ""),
    )


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def observer_poll_timeline_payload(
    *,
    observer_command_id: str = "",
    command: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    event: str = "",
) -> dict[str, Any]:
    """Build route-bound task_timeline payload for observer poll events."""

    command_payload = _command_payload(command)
    plan_payload = dict(plan) if isinstance(plan, Mapping) else {}
    result_payload = dict(result) if isinstance(result, Mapping) else {}
    route_identity = (
        plan_payload.get("route_identity")
        if isinstance(plan_payload.get("route_identity"), Mapping)
        else {}
    )
    timeline = {
        "schema_version": OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION,
        "event": str(event or ""),
        "observer_command_id": str(
            _first_non_empty(
                observer_command_id,
                plan_payload.get("observer_command_id"),
                result_payload.get("observer_command_id"),
                command.get("command_id") if isinstance(command, Mapping) else "",
            )
            or ""
        ),
        "backlog_id": str(
            _first_non_empty(
                plan_payload.get("backlog_id"),
                result_payload.get("backlog_id"),
                command_payload.get("backlog_id"),
            )
            or ""
        ),
        "payload_free_reminder": True,
        "reminder_payload_required": False,
    }
    for field in OBSERVER_POLL_TIMELINE_ROUTE_FIELDS:
        timeline[field] = str(
            _first_non_empty(
                route_identity.get(field),
                result_payload.get(field),
                command_payload.get(field),
            )
            or ""
        )
    for field in OBSERVER_POLL_TIMELINE_FLAG_FIELDS:
        value = _first_non_empty(
            plan_payload.get(field),
            result_payload.get(field),
            command_payload.get(field),
            False,
        )
        timeline[field] = bool(value)
    return timeline


def _observer_poll_base_result(request: ObserverPollRequest) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": OBSERVER_POLL_SCHEMA_VERSION,
        "project_id": request.project_id,
        "observer_session_id": request.observer_session_id,
        "execute": False,
        "calls_models": False,
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
    }


def _observer_poll_terminal_blocker(
    *,
    request: ObserverPollRequest,
    observer_command_id: str,
    backlog_id: str,
    route_identity: Mapping[str, Any],
    observer_result: Mapping[str, Any],
) -> dict[str, Any]:
    invocation = (
        observer_result.get("invocation")
        or observer_result.get("invocation_request")
        or {}
    )
    invocation = invocation if isinstance(invocation, Mapping) else {}
    blocker_id = str(
        observer_result.get("divergence_reason")
        or invocation.get("blocker_id")
        or invocation.get("auth_status")
        or observer_result.get("status")
        or "observer_command_terminal_blocker"
    )
    return {
        "schema_version": "observer_command_terminal_blocker.v1",
        "ok": False,
        "status": "blocked",
        "terminal_dispatch_blocker": True,
        "blocker_id": blocker_id,
        "observer_command_id": observer_command_id,
        "backlog_id": backlog_id,
        "route_id": str(route_identity.get("route_id") or ""),
        "route_context_hash": str(route_identity.get("route_context_hash") or ""),
        "prompt_contract_id": str(route_identity.get("prompt_contract_id") or ""),
        "prompt_contract_hash": str(route_identity.get("prompt_contract_hash") or ""),
        "visible_injection_manifest_hash": str(
            route_identity.get("visible_injection_manifest_hash") or ""
        ),
        "backend_mode": request.backend_mode,
        "auth_status": str(invocation.get("auth_status") or "blocked"),
        "startup_recorded": False,
        "read_receipt_recorded": False,
        "command_projection_status": "failed",
        "canonical_contract_state": "blocked",
        "failure_evidence_appended": True,
        "reason": (
            "observer command reached a terminal blocker before startup/read receipt "
            "could become close-satisfying evidence"
        ),
    }


def build_observer_poll_plan(
    request: ObserverPollRequest,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    """Convert a claimed observer command into a route-bound observer plan.

    This is the standalone observer-session entrypoint: it consumes the durable
    command payload directly and deliberately avoids ServiceManager, executor,
    and task_create dependencies.
    """

    result = _observer_poll_base_result(request)
    result["execute"] = execute
    command = request.command
    if not command:
        result.update(
            {
                "ok": True,
                "status": "empty",
                "empty": True,
                "auth_status": "not_invoked",
            }
        )
        return result

    command_id = str(command.get("command_id") or "")
    command_type = str(command.get("command_type") or "")
    payload = _command_payload(command)
    backlog_id = str(payload.get("backlog_id") or "")
    route = _route_from_command_payload(payload)
    route_identity = {
        "route_id": str(payload.get("route_id") or ""),
        "route_context_hash": route.route_context_hash,
        "prompt_contract_id": route.prompt_contract_id,
        "prompt_contract_hash": route.prompt_contract_hash,
        "route_token_ref": route.route_token_ref,
        "visible_injection_manifest_hash": str(
            payload.get("visible_injection_manifest_hash") or ""
        ),
        "raw_private_context_exposed": False,
    }
    result.update(
        {
            "status": "rejected",
            "empty": False,
            "observer_command_id": command_id,
            "command_type": command_type,
            "command_status": str(command.get("status") or ""),
            "backlog_id": backlog_id,
            "route_identity": route_identity,
            "payload_keys": sorted(str(key) for key in payload.keys()),
        }
    )
    if command_type != EXECUTE_BACKLOG_ROW_COMMAND_TYPE:
        result["error"] = (
            "observer poll supports only execute_backlog_row commands in standalone mode"
        )
        return result

    missing = _missing_execute_backlog_payload_fields(payload)
    if missing:
        result["missing"] = missing
        result["error"] = "execute_backlog_row payload is missing route/backlog fields"
        return result

    observer_request = ObserverRunRequest(
        project_id=request.project_id,
        backlog_id=backlog_id,
        route=route,
        provider=request.provider,
        model=request.model,
        backend_mode=request.backend_mode,
        workspace=request.workspace or str(Path.cwd()),
        prompt=request.prompt,
        timeout_sec=request.timeout_sec,
        early_progress_timeout_sec=request.early_progress_timeout_sec,
        dispatch_gate=request.dispatch_gate,
        main_worktree=request.main_worktree or str(Path.cwd()),
        heartbeat_callback=request.heartbeat_callback,
        heartbeat_interval_sec=request.heartbeat_interval_sec,
    )
    observer_result = run_observer(observer_request, execute=execute)
    invocation = (
        observer_result.get("invocation")
        or observer_result.get("invocation_request")
        or {}
    )
    result.update(
        {
            "ok": bool(observer_result.get("ok")),
            "status": observer_result.get("status") or "planned",
            "observer_run": observer_result,
            "planned_invocation": invocation,
            "calls_models": bool(invocation.get("calls_models")),
            "auth_status": invocation.get("auth_status", "not_invoked"),
        }
    )
    if not result["ok"]:
        result["missing"] = observer_result.get("missing") or []
        if execute and observer_result.get("status") == "blocked":
            terminal_blocker = _observer_poll_terminal_blocker(
                request=request,
                observer_command_id=command_id,
                backlog_id=backlog_id,
                route_identity=route_identity,
                observer_result=observer_result,
            )
            result["terminal_dispatch_blocker"] = True
            result["terminal_contract_projection"] = {
                "schema_version": "observer_command_terminal_projection.v1",
                "passed": False,
                "canonical_contract_state": "blocked",
                "command_projection_status": "failed",
                "divergence_reason": terminal_blocker["blocker_id"],
                "observer_command_id": command_id,
            }
            result["failure_evidence"] = terminal_blocker
            result["command_projection_status"] = "failed"
            result["canonical_contract_state"] = "blocked"
    return result


def _stable_suffix(*parts: str, length: int = 12) -> str:
    payload = "\n".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _runtime_text_secret_hash(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _runtime_text_test_environment_preflight(
    test_commands: Sequence[str],
) -> dict[str, Any]:
    original_commands = [
        str(item or "").strip()
        for item in test_commands
        if str(item or "").strip()
    ]
    normalized_commands: list[str] = []
    pytest_required = False

    for command in original_commands:
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        if not parts:
            continue
        runner = parts[0]
        rewritten_parts = list(parts)
        if runner in {"pytest", "py.test"}:
            pytest_required = True
            rewritten_parts[0] = ".venv/bin/pytest"
        elif runner.endswith("/pytest") or runner.endswith("/py.test"):
            pytest_required = True
        elif (
            runner in {"python", "python3"}
            and len(parts) >= 3
            and parts[1:3] == ["-m", "pytest"]
        ):
            pytest_required = True
            rewritten_parts[0] = ".venv/bin/python"
        elif any(part == "pytest" or part.endswith("/pytest") for part in parts):
            pytest_required = True
        normalized_commands.append(
            " ".join(shlex.quote(part) for part in rewritten_parts)
        )

    setup_commands: list[str] = []
    if pytest_required:
        setup_commands = [
            (
                "if [ ! -x .venv/bin/python ] || "
                "! .venv/bin/python -m pytest --version >/dev/null 2>&1 || "
                "! .venv/bin/python -c \"import yaml\" >/dev/null 2>&1; then "
                "python3 -m venv .venv && "
                "if [ -f agent/requirements.txt ]; then "
                ".venv/bin/python -m pip install -r agent/requirements.txt; "
                "fi && "
                ".venv/bin/python -m pip install pytest; "
                "fi"
            ),
            ".venv/bin/python -m pytest --version",
        ]

    return {
        "schema_version": "observer_worker_launch_pack.test_environment_preflight.v1",
        "status": "required" if pytest_required else "not_required",
        "run_before": "run_focused_tests",
        "working_directory": "assigned_worktree",
        "scope": "assigned_worktree_only",
        "setup_commands": setup_commands,
        "original_test_commands": original_commands,
        "test_commands": normalized_commands,
        "dependency_sources": ["agent/requirements.txt"] if pytest_required else [],
        "package_installs": ["pytest"] if pytest_required else [],
        "install_when_missing": pytest_required,
        "may_use_network": pytest_required,
        "raw_tokens_persisted": False,
    }


def _normalize_path(path: str) -> str:
    token = str(path or "").strip()
    if not token:
        return ""
    return str(Path(token).expanduser().resolve())


def _runtime_text_git_dir(worktree_path: str) -> str:
    token = str(worktree_path or "").strip()
    if not token:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", token, "rev-parse", "--git-dir"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    git_dir = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not git_dir:
        return ""
    path = Path(git_dir)
    if not path.is_absolute():
        path = Path(token) / path
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return ""


def _git_head(path: Path) -> str:
    try:
        return batch_jobs.git_commit(path)
    except Exception:
        return ""


def _git_worktree_status(worktree: str | Path, *, main_worktree: str | Path) -> dict[str, Any]:
    normalized_worktree = _normalize_path(str(worktree))
    normalized_main = _normalize_path(str(main_worktree))
    git_marker = Path(normalized_worktree) / ".git" if normalized_worktree else Path("")
    marker_exists = bool(normalized_worktree) and (
        git_marker.is_file() or git_marker.is_dir()
    )
    differs_from_main = bool(normalized_worktree and normalized_main) and (
        normalized_worktree != normalized_main
    )
    return {
        "worktree": normalized_worktree,
        "main_worktree": normalized_main,
        "exists": Path(normalized_worktree).is_dir() if normalized_worktree else False,
        "git_marker_exists": marker_exists,
        "differs_from_main_worktree": differs_from_main,
        "is_git_worktree": marker_exists and differs_from_main,
    }


def _git_output_or_empty(args: Sequence[str], *, cwd: str | Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_current_branch(path: str | Path) -> str:
    branch = _git_output_or_empty(["branch", "--show-current"], cwd=path)
    return branch or _git_output_or_empty(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)


def _git_status_short_files(path: str | Path) -> list[str]:
    output = _git_output_or_empty(["status", "--short"], cwd=path)
    files: list[str] = []
    for line in output.splitlines():
        token = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in token:
            token = token.split(" -> ", 1)[1].strip()
        if token:
            files.append(token)
    return sorted(set(files))


def _worker_worktree_diff_scope(
    *,
    worktree: str | Path,
    base_commit: str,
    owned_files: Sequence[str],
) -> dict[str, Any]:
    worktree_path = str(Path(str(worktree)).expanduser().resolve())
    committed_changed: list[str] = []
    diff_error = ""
    if base_commit:
        try:
            committed_changed = batch_jobs.git_changed_files(
                worktree_path,
                base_ref=base_commit,
            )
        except Exception as exc:
            diff_error = str(exc)
    dirty_files = _git_status_short_files(worktree_path)
    all_changed = sorted(set(committed_changed) | set(dirty_files))
    tool_artifact_files = [
        path
        for path in all_changed
        if path == ".aming-claw" or path.startswith(".aming-claw/")
    ]
    implementation_changed = [
        path for path in all_changed if path not in set(tool_artifact_files)
    ]
    owned = {str(item) for item in owned_files if str(item or "").strip()}
    out_of_scope = [path for path in all_changed if owned and path not in owned]
    return {
        "schema_version": "mf_subagent_worktree_diff_scope.v1",
        "worktree": worktree_path,
        "base_commit": base_commit,
        "head_commit": _git_head(Path(worktree_path)),
        "committed_changed_files": committed_changed,
        "dirty_files": dirty_files,
        "changed_files": all_changed,
        "implementation_changed_files": implementation_changed,
        "tool_artifact_files": tool_artifact_files,
        "no_diff": not implementation_changed,
        "worktree_clean": not dirty_files,
        "dirty_scope_exact_match": not out_of_scope,
        "owned_files": sorted(owned),
        "out_of_scope_files": out_of_scope,
        "diff_error": diff_error,
    }


def _runtime_monitor_summary(monitor: Mapping[str, Any]) -> dict[str, Any]:
    monitor = monitor if isinstance(monitor, Mapping) else {}
    early_progress = monitor.get("early_progress")
    early_progress = early_progress if isinstance(early_progress, Mapping) else {}
    return {
        "schema_version": "observer_cli_runtime_monitor_summary.v1",
        "present": bool(monitor),
        "monitor_schema_version": str(monitor.get("schema_version") or ""),
        "early_progress_timeout_sec": monitor.get("early_progress_timeout_sec", 0),
        "heartbeat_enabled": bool(monitor.get("heartbeat_enabled")),
        "heartbeat_count": monitor.get("heartbeat_count", 0),
        "heartbeat_failures": monitor.get("heartbeat_failures", 0),
        "progress_observed": bool(monitor.get("progress_observed")),
        "early_progress_observed": bool(early_progress.get("progress_observed")),
        "stdout_bytes": early_progress.get("stdout_bytes", 0),
        "stderr_bytes": early_progress.get("stderr_bytes", 0),
        "dirty_files": list(early_progress.get("dirty_files") or []),
        "changed_files": list(early_progress.get("changed_files") or []),
    }


def _dogfood_execute_launch_env(
    *,
    request: "DogfoodObserverPlanRequest",
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
) -> dict[str, Any]:
    """Build child-process env additions without persisting raw secret values."""

    session_token = str(os.environ.get("AMING_WORKER_SESSION_TOKEN") or "").strip()
    env = {
        "AMING_GOVERNANCE_URL": str(os.environ.get("AMING_GOVERNANCE_URL") or "http://localhost:40000"),
        "AMING_RUNTIME_CONTEXT_ID": runtime_context_id,
        "AMING_OBSERVER_COMMAND_ID": observer_command_id,
        "AMING_WORKER_TASK_ID": str(getattr(context, "task_id", "") or request.task_id or ""),
        "AMING_WORKER_FENCE_TOKEN": str(getattr(context, "fence_token", "") or request.fence_token or ""),
    }
    if session_token:
        env["AMING_WORKER_SESSION_TOKEN"] = session_token
    missing = [
        key
        for key in (
            "AMING_WORKER_SESSION_TOKEN",
            "AMING_WORKER_FENCE_TOKEN",
            "AMING_RUNTIME_CONTEXT_ID",
        )
        if not str(env.get(key) or "").strip()
    ]
    return {
        "schema_version": "observer_dogfood_execute_launch_env.v1",
        "allowed": not missing,
        "env": env,
        "env_keys": sorted(env),
        "missing_env": missing,
        "session_token_present": bool(session_token),
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
    }


def _dogfood_execute_env_blocker(
    *,
    request: "DogfoodObserverPlanRequest",
    context: Any,
    launch_env: Mapping[str, Any],
    executable_worker_launch: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "observer_dogfood_execute_env_blocker.v1",
        "ok": False,
        "status": "blocked",
        "terminal_dispatch_blocker": True,
        "blocker_id": "worker_session_token_env_missing_before_cli_launch",
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": str(getattr(context, "task_id", "") or request.task_id or ""),
        "runtime_context_id": request.runtime_context_id
        or runtime_context_id_for_branch_context(context),
        "missing_env": list(launch_env.get("missing_env") or []),
        "env_keys": list(launch_env.get("env_keys") or []),
        "executable_worker_launch": dict(executable_worker_launch),
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "reason": (
            "execute requested for a bounded CLI worker, but the parent observer "
            "process did not provide the server-issued AMING_WORKER_SESSION_TOKEN "
            "needed for worker read-receipt/startup facades."
        ),
        "next_action": (
            "mint/allocate the worker session token, then launch observer dogfood "
            "with AMING_WORKER_SESSION_TOKEN set only in process env."
        ),
    }


def _dogfood_secret_redacted_copy(
    value: Any,
    *,
    session_token: str = "",
    fence_token: str = "",
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _dogfood_secret_redacted_copy(
                nested,
                session_token=session_token,
                fence_token=fence_token,
            )
            for key, nested in value.items()
            if str(key) not in {"session_token"}
        }
    if isinstance(value, list):
        return [
            _dogfood_secret_redacted_copy(
                item,
                session_token=session_token,
                fence_token=fence_token,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _dogfood_secret_redacted_copy(
                item,
                session_token=session_token,
                fence_token=fence_token,
            )
            for item in value
        ]
    if isinstance(value, str):
        text = value
        if session_token:
            text = text.replace(
                session_token,
                "<read from env:AMING_WORKER_SESSION_TOKEN at submission time>",
            )
        if fence_token:
            text = text.replace(fence_token, _runtime_text_secret_hash(fence_token))
        return text
    return value


def _dogfood_fill_read_receipt_body(
    value: Any,
    *,
    session_token: str,
    fence_token: str,
    launch_text_hash: str,
) -> Any:
    if isinstance(value, Mapping):
        filled: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            if key == "session_token":
                filled[key] = session_token
            elif key == "fence_token":
                filled[key] = fence_token
            elif key in {"read_receipt_hash", "launch_text_hash"}:
                nested_text = str(nested or "").strip()
                filled[key] = (
                    launch_text_hash
                    if not nested_text or nested_text.startswith("<")
                    else nested
                )
            else:
                filled[key] = _dogfood_fill_read_receipt_body(
                    nested,
                    session_token=session_token,
                    fence_token=fence_token,
                    launch_text_hash=launch_text_hash,
                )
        return filled
    if isinstance(value, list):
        return [
            _dogfood_fill_read_receipt_body(
                item,
                session_token=session_token,
                fence_token=fence_token,
                launch_text_hash=launch_text_hash,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _dogfood_fill_read_receipt_body(
                item,
                session_token=session_token,
                fence_token=fence_token,
                launch_text_hash=launch_text_hash,
            )
            for item in value
        ]
    return value


def _dogfood_submit_read_receipt_facade(
    *,
    request: "DogfoodObserverPlanRequest",
    context: Any,
    launch_env: Mapping[str, Any],
    runtime_text: Mapping[str, Any],
    executable_worker_launch: Mapping[str, Any],
) -> dict[str, Any]:
    if bool(runtime_text.get("read_receipt_recorded")):
        return {
            "schema_version": "observer_dogfood_read_receipt_submission.v1",
            "ok": True,
            "status": "already_recorded",
            "read_receipt_recorded": True,
            "read_receipt_timeline_event_id": str(
                runtime_text.get("read_receipt_event_id") or ""
            ),
            "read_receipt_hash": str(runtime_text.get("read_receipt_hash") or ""),
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
        }

    env = launch_env.get("env") if isinstance(launch_env.get("env"), Mapping) else {}
    session_token = str(env.get("AMING_WORKER_SESSION_TOKEN") or "").strip()
    fence_token = str(env.get("AMING_WORKER_FENCE_TOKEN") or "").strip()
    launch_text_hash = str(runtime_text.get("launch_text_hash") or "").strip()
    handoff = (
        executable_worker_launch.get("handoff_packet")
        if isinstance(executable_worker_launch.get("handoff_packet"), Mapping)
        else {}
    )
    skeleton = (
        handoff.get("read_receipt_facade_payload_skeleton")
        if isinstance(handoff.get("read_receipt_facade_payload_skeleton"), Mapping)
        else {}
    )
    copy_safe_body = (
        skeleton.get("copy_safe_body")
        if isinstance(skeleton.get("copy_safe_body"), Mapping)
        else skeleton.get("body")
        if isinstance(skeleton.get("body"), Mapping)
        else {}
    )
    path = str(skeleton.get("path") or "").strip()
    if not session_token or not fence_token or not launch_text_hash or not copy_safe_body or not path:
        missing = [
            name
            for name, value in (
                ("AMING_WORKER_SESSION_TOKEN", session_token),
                ("AMING_WORKER_FENCE_TOKEN", fence_token),
                ("launch_text_hash", launch_text_hash),
                ("read_receipt_facade_payload_skeleton.copy_safe_body", copy_safe_body),
                ("read_receipt_facade_payload_skeleton.path", path),
            )
            if not value
        ]
        return {
            "schema_version": "observer_dogfood_read_receipt_submission.v1",
            "ok": False,
            "status": "blocked",
            "blocker_id": "read_receipt_facade_payload_incomplete",
            "missing_fields": missing,
            "read_receipt_recorded": False,
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
        }

    body = _dogfood_fill_read_receipt_body(
        copy_safe_body,
        session_token=session_token,
        fence_token=fence_token,
        launch_text_hash=launch_text_hash,
    )
    body["actor"] = str(
        getattr(context, "worker_slot_id", "")
        or getattr(context, "worker_id", "")
        or request.worker_id
        or "mf_sub"
    )
    body.setdefault("event_type", "mf_subagent_read_receipt")
    body.setdefault("event_kind", "mf_subagent_read_receipt")
    body.setdefault("status", "accepted")
    path = path if path.startswith("/") else f"/{path}"
    base_url = str(env.get("AMING_GOVERNANCE_URL") or "http://localhost:40000").rstrip("/")
    url = f"{base_url}{path}"
    data = json.dumps(body, sort_keys=True).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=str(skeleton.get("method") or "POST"),
    )
    public_request = {
        "method": str(skeleton.get("method") or "POST"),
        "url": url,
        "body": _dogfood_secret_redacted_copy(
            body,
            session_token=session_token,
            fence_token=fence_token,
        ),
    }
    try:
        with urllib.request.urlopen(http_request, timeout=10) as response:  # noqa: S310 - local governance URL
            response_body = response.read().decode("utf-8")
            status_code = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        payload: Any
        try:
            payload = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            payload = {"raw_body": response_body}
        return {
            "schema_version": "observer_dogfood_read_receipt_submission.v1",
            "ok": False,
            "status": "http_error",
            "blocker_id": "read_receipt_facade_submit_failed",
            "status_code": int(exc.code),
            "request": public_request,
            "response": _dogfood_secret_redacted_copy(
                payload,
                session_token=session_token,
                fence_token=fence_token,
            ),
            "read_receipt_recorded": False,
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
        }
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {
            "schema_version": "observer_dogfood_read_receipt_submission.v1",
            "ok": False,
            "status": "network_error",
            "blocker_id": "read_receipt_facade_submit_failed",
            "error": str(exc),
            "request": public_request,
            "read_receipt_recorded": False,
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
        }

    try:
        payload = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError:
        payload = {"raw_body": response_body}
    payload = payload if isinstance(payload, Mapping) else {}
    event = payload.get("timeline_event") if isinstance(payload.get("timeline_event"), Mapping) else {}
    receipt = payload.get("read_receipt") if isinstance(payload.get("read_receipt"), Mapping) else {}
    event_id = str(event.get("id") or event.get("event_id") or "")
    read_receipt_hash = str(
        receipt.get("read_receipt_hash")
        or receipt.get("launch_text_hash")
        or body.get("read_receipt_hash")
        or body.get("launch_text_hash")
        or ""
    )
    ok = bool(payload.get("ok", 200 <= status_code < 300)) and bool(event_id)
    return {
        "schema_version": "observer_dogfood_read_receipt_submission.v1",
        "ok": ok,
        "status": "submitted" if ok else "blocked",
        "blocker_id": "" if ok else "read_receipt_facade_submit_failed",
        "status_code": status_code,
        "request": public_request,
        "response": _dogfood_secret_redacted_copy(
            payload,
            session_token=session_token,
            fence_token=fence_token,
        ),
        "read_receipt_recorded": ok,
        "read_receipt_timeline_event_id": event_id,
        "read_receipt_hash": read_receipt_hash,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
    }


def _dogfood_read_receipt_submission_blocker(
    *,
    request: "DogfoodObserverPlanRequest",
    context: Any,
    submission: Mapping[str, Any],
    executable_worker_launch: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "observer_dogfood_read_receipt_submission_blocker.v1",
        "ok": False,
        "status": "blocked",
        "terminal_dispatch_blocker": True,
        "blocker_id": str(
            submission.get("blocker_id") or "read_receipt_facade_submit_failed"
        ),
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": str(getattr(context, "task_id", "") or request.task_id or ""),
        "runtime_context_id": request.runtime_context_id
        or runtime_context_id_for_branch_context(context),
        "read_receipt_submission": dict(submission),
        "executable_worker_launch": dict(executable_worker_launch),
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "reason": (
            "execute requested for a bounded CLI worker, but the worker read "
            "receipt facade could not be recorded before provider invocation."
        ),
        "next_action": (
            "repair the runtime-context read receipt facade request, then retry "
            "the bounded worker launch without invoking the provider first."
        ),
    }


def _startup_read_receipt_recording_status(
    observer_result: Mapping[str, Any],
) -> dict[str, Any]:
    observer_result = observer_result if isinstance(observer_result, Mapping) else {}
    startup_event = observer_result.get("startup_timeline_event")
    startup_event = startup_event if isinstance(startup_event, Mapping) else {}
    startup_recording = observer_result.get("startup_recording")
    startup_recording = startup_recording if isinstance(startup_recording, Mapping) else {}
    startup_payload = startup_event.get("payload")
    startup_payload = startup_payload if isinstance(startup_payload, Mapping) else {}
    startup_gate = startup_payload.get("mf_subagent_startup_gate")
    startup_gate = startup_gate if isinstance(startup_gate, Mapping) else {}
    read_receipt = observer_result.get("read_receipt")
    read_receipt = read_receipt if isinstance(read_receipt, Mapping) else {}
    startup_recording_append = observer_result.get("startup_recording_append")
    startup_recording_append = (
        startup_recording_append
        if isinstance(startup_recording_append, Mapping)
        else {}
    )
    read_receipt_recording_append = observer_result.get("read_receipt_recording_append")
    read_receipt_recording_append = (
        read_receipt_recording_append
        if isinstance(read_receipt_recording_append, Mapping)
        else {}
    )
    return {
        "schema_version": "observer_startup_read_receipt_recording_status.v1",
        "startup_prepared": bool(
            startup_event
            or startup_recording
            or observer_result.get("startup_evidence_appendable")
        ),
        "startup_appendable": bool(
            startup_event or observer_result.get("startup_evidence_appendable")
        ),
        "startup_recorded": bool(observer_result.get("actual_startup_recorded")),
        "startup_timeline_event_recorded": bool(
            observer_result.get("timeline_event_recorded")
        ),
        "startup_timeline_event_id": (
            startup_recording.get("timeline_event_id")
            or startup_recording_append.get("event_id")
            or startup_event.get("id")
            or startup_event.get("event_id")
            or ""
        ),
        "startup_event_kind": str(startup_event.get("event_kind") or ""),
        "startup_status": str(startup_event.get("status") or ""),
        "startup_close_satisfying": bool(
            startup_recording.get("close_satisfying")
            or startup_gate.get("close_satisfying")
        ),
        "startup_counts_as_real_worker_evidence": bool(
            startup_recording.get("counts_as_real_worker_evidence")
            or startup_gate.get("counts_as_real_worker_evidence")
        ),
        "startup_surrogate_not_close_satisfying": bool(
            startup_recording.get("host_adapter_startup_surrogate_not_close_satisfying")
            or startup_gate.get("host_adapter_startup_surrogate_not_close_satisfying")
        ),
        "read_receipt_prepared": bool(read_receipt),
        "read_receipt_recorded": bool(observer_result.get("read_receipt_recorded")),
        "read_receipt_recorded_before_implementation_wait": bool(
            observer_result.get("read_receipt_recorded_before_implementation_wait")
        ),
        "read_receipt_timeline_event_id": (
            read_receipt.get("timeline_event_id")
            or read_receipt_recording_append.get("event_id")
            or ""
        ),
        "read_receipt_hash": str(
            read_receipt.get("read_receipt_hash") or read_receipt.get("hash") or ""
        ),
        "post_hoc_read_receipt_satisfies_gate": False,
        "implementation_evidence_recorded": False,
        "close_ready": False,
    }


def _merge_startup_read_receipt_recording_status(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base or {})
    overlay = overlay if isinstance(overlay, Mapping) else {}
    for key, value in overlay.items():
        if key == "schema_version":
            continue
        if isinstance(value, bool):
            merged[key] = bool(merged.get(key)) or value
        elif isinstance(value, list):
            existing = list(merged.get(key) or [])
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged[key] = existing
        elif value not in ("", None, [], {}):
            merged[key] = value
    return merged


def _timeline_deep_text(value: Any, field: str) -> str:
    stack: list[Any] = [value]
    seen: set[int] = set()
    while stack:
        current = stack.pop(0)
        if isinstance(current, Mapping):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            direct = current.get(field)
            if direct not in (None, "", [], {}):
                return str(direct).strip()
            for nested in current.values():
                if isinstance(nested, (Mapping, list, tuple)):
                    stack.append(nested)
        elif isinstance(current, (list, tuple)):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            stack.extend(
                item for item in current if isinstance(item, (Mapping, list, tuple))
            )
    return ""


def _timeline_event_route_identity(event: Mapping[str, Any]) -> dict[str, str]:
    fields = (
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
    )
    identity = {
        field: _timeline_deep_text(event, field)
        for field in fields
        if _timeline_deep_text(event, field)
    }
    route_id = _timeline_deep_text(event, "route_id")
    if route_id:
        identity["route_id"] = route_id
    return identity


def _timeline_event_attempt_lineage(event: Mapping[str, Any]) -> dict[str, str]:
    lineage = {
        "runtime_context_id": _timeline_deep_text(event, "runtime_context_id"),
        "task_id": _timeline_deep_text(event, "task_id"),
        "parent_task_id": _timeline_deep_text(event, "parent_task_id"),
    }
    if not lineage["parent_task_id"]:
        lineage["parent_task_id"] = str(event.get("backlog_id") or "").strip()
    return {key: value for key, value in lineage.items() if value}


def _timeline_mapping_matches_filter(
    value: Mapping[str, str],
    expected: Mapping[str, str],
) -> bool:
    return all(
        str(value.get(key) or "").strip() == str(match or "").strip()
        for key, match in expected.items()
        if str(match or "").strip()
    )


def _timeline_event_ref(event: Mapping[str, Any], *, reason: str = "") -> dict[str, Any]:
    ref = {
        "id": event.get("id") or event.get("event_id"),
        "event_kind": event.get("event_kind"),
        "event_type": event.get("event_type"),
        "phase": event.get("phase"),
        "status": event.get("status") or event.get("decision"),
    }
    if reason:
        ref["reason"] = reason
    return {key: value for key, value in ref.items() if value not in (None, "")}


def _timeline_startup_events_for_lineage(
    events: Sequence[Mapping[str, Any]],
    *,
    identity_filter: Mapping[str, str],
    attempt_lineage_filter: Mapping[str, str],
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    matched: list[Mapping[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for event in events:
        identity = _timeline_event_route_identity(event)
        if identity_filter:
            if not identity:
                ignored.append(
                    _timeline_event_ref(
                        event,
                        reason="missing_route_identity_for_current_lineage",
                    )
                )
                continue
            if not _timeline_mapping_matches_filter(identity, identity_filter):
                ignored.append(
                    _timeline_event_ref(event, reason="superseded_route_identity")
                )
                continue
        attempt_lineage = _timeline_event_attempt_lineage(event)
        if attempt_lineage_filter:
            if not attempt_lineage:
                ignored.append(
                    _timeline_event_ref(
                        event,
                        reason="missing_attempt_lineage_for_current_route",
                    )
                )
                continue
            if not _timeline_mapping_matches_filter(
                attempt_lineage,
                attempt_lineage_filter,
            ):
                ignored.append(
                    _timeline_event_ref(event, reason="superseded_attempt_lineage")
                )
                continue
        matched.append(event)
    return matched, ignored


def _timeline_startup_read_receipt_recording_status(
    *,
    project_id: str,
    backlog_id: str,
    task_id: str,
    runtime_context_id: str,
    parent_task_id: str,
    route_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Return recorded startup/read-receipt facts for a worker task.

    Timeout handling may run after the worker has already written facade
    evidence. Re-read the authoritative timeline so the terminal projection does
    not report a missing read receipt that already exists.
    """

    if not project_id or not task_id:
        return {}
    try:
        from governance import task_timeline
        from governance.db import DBContext
    except ImportError:  # pragma: no cover - package import path
        from agent.governance import task_timeline
        from agent.governance.db import DBContext

    try:
        with DBContext(project_id) as conn:
            events = task_timeline.list_events(
                conn,
                project_id,
                task_id=task_id,
                backlog_id=backlog_id,
                limit=1000,
            )
    except Exception as exc:
        return {
            "schema_version": "observer_startup_read_receipt_timeline_status.v1",
            "timeline_status_error": str(exc),
        }

    lineage_filter = {
        "route_context_hash": str(route_identity.get("route_context_hash") or ""),
        "prompt_contract_id": str(route_identity.get("prompt_contract_id") or ""),
        "prompt_contract_hash": str(route_identity.get("prompt_contract_hash") or ""),
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
    }
    read_gate = task_timeline.mf_subagent_read_receipt_gate_verification(
        events,
        route_identity_filter={
            key: value for key, value in lineage_filter.items() if value
        },
    )
    read_event_id = str(read_gate.get("read_receipt_event_id") or "")
    read_receipt_recorded = bool(read_event_id)
    startup_events = [
        event
        for event in events
        if str(event.get("event_kind") or event.get("event_type") or "")
        == "mf_subagent_startup"
    ]
    identity_filter = {
        key: value
        for key, value in lineage_filter.items()
        if key in {"route_context_hash", "prompt_contract_id", "prompt_contract_hash"}
        and value
    }
    attempt_lineage_filter = {
        key: value
        for key, value in lineage_filter.items()
        if key in {"runtime_context_id", "task_id", "parent_task_id"} and value
    }
    matched_startup_events, ignored_startup_events = (
        _timeline_startup_events_for_lineage(
            startup_events,
            identity_filter=identity_filter,
            attempt_lineage_filter=attempt_lineage_filter,
        )
    )
    startup_event = matched_startup_events[-1] if matched_startup_events else {}
    startup_payload = (
        startup_event.get("payload") if isinstance(startup_event, Mapping) else {}
    )
    startup_payload = startup_payload if isinstance(startup_payload, Mapping) else {}
    startup_gate = startup_payload.get("mf_subagent_startup_gate")
    startup_gate = startup_gate if isinstance(startup_gate, Mapping) else {}
    return {
        "schema_version": "observer_startup_read_receipt_timeline_status.v1",
        "timeline_events_checked": len(events),
        "timeline_read_receipt_gate": read_gate,
        "read_receipt_recorded": read_receipt_recorded,
        "read_receipt_recorded_before_implementation_wait": read_receipt_recorded,
        "read_receipt_timeline_event_id": read_event_id,
        "read_receipt_hash": str(read_gate.get("read_receipt_hash") or ""),
        "read_receipt_prepared": read_receipt_recorded,
        "startup_recorded": bool(startup_event),
        "startup_timeline_event_recorded": bool(startup_event),
        "startup_timeline_event_id": str(
            startup_event.get("id") or startup_event.get("event_id") or ""
        ),
        "startup_event_kind": str(startup_event.get("event_kind") or ""),
        "startup_status": str(startup_event.get("status") or ""),
        "startup_close_satisfying": bool(
            startup_gate.get("close_satisfying")
            or startup_payload.get("close_satisfying")
        ),
        "startup_counts_as_real_worker_evidence": bool(
            startup_gate.get("counts_as_real_worker_evidence")
            or startup_payload.get("counts_as_real_worker_evidence")
        ),
        "timeline_read_receipt_event_ids": [
            event.get("id")
            for event in events
            if str(event.get("event_kind") or event.get("event_type") or "")
            == "mf_subagent_read_receipt"
            and event.get("id") is not None
        ],
        "timeline_startup_event_ids": [
            event.get("id") for event in startup_events if event.get("id") is not None
        ],
        "timeline_startup_matched_event_ids": [
            event.get("id")
            for event in matched_startup_events
            if event.get("id") is not None
        ],
        "timeline_startup_ignored_event_ids": [
            item.get("id") for item in ignored_startup_events if item.get("id") is not None
        ],
        "timeline_startup_ignored_events": ignored_startup_events,
    }


def _record_task_timeline_event(
    *,
    project_id: str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        from governance import task_timeline
        from governance.db import DBContext
    except ImportError:  # pragma: no cover - package import path
        from agent.governance import task_timeline
        from agent.governance.db import DBContext

    with DBContext(project_id) as conn:
        task_timeline.ensure_schema(conn)
        return task_timeline.record_event(
            conn,
            project_id=project_id,
            task_id=str(event.get("task_id") or ""),
            backlog_id=str(event.get("backlog_id") or ""),
            mf_id=str(event.get("mf_id") or ""),
            attempt_num=int(event.get("attempt_num") or 0),
            event_type=str(event.get("event_type") or ""),
            phase=str(event.get("phase") or ""),
            event_kind=str(event.get("event_kind") or ""),
            scenario_id=str(event.get("scenario_id") or ""),
            parent_event_id=int(event.get("parent_event_id") or 0),
            correlation_id=str(event.get("correlation_id") or ""),
            severity=str(event.get("severity") or ""),
            decision=str(event.get("decision") or ""),
            schema_version=int(event.get("schema_version") or 2),
            actor=str(event.get("actor") or ""),
            status=str(event.get("status") or ""),
            payload=dict(event.get("payload") or {}),
            verification=dict(event.get("verification") or {}),
            artifact_refs=dict(event.get("artifact_refs") or {}),
            trace_id=str(event.get("trace_id") or ""),
            commit_sha=str(event.get("commit_sha") or ""),
        )


def _dogfood_terminal_blocker_timeline_event(
    blocker: Mapping[str, Any],
) -> dict[str, Any]:
    blocker = blocker if isinstance(blocker, Mapping) else {}
    diff_scope = blocker.get("worktree_diff_scope")
    diff_scope = diff_scope if isinstance(diff_scope, Mapping) else {}
    runtime_context = blocker.get("runtime_context")
    runtime_context = runtime_context if isinstance(runtime_context, Mapping) else {}
    route_identity = blocker.get("route_identity")
    route_identity = route_identity if isinstance(route_identity, Mapping) else {}
    startup_status = blocker.get("startup_read_receipt_recording_status")
    startup_status = startup_status if isinstance(startup_status, Mapping) else {}
    executable_worker_launch = blocker.get("executable_worker_launch")
    executable_worker_launch = (
        executable_worker_launch if isinstance(executable_worker_launch, Mapping) else {}
    )
    event_payload = {
        "schema_version": "observer_dogfood_terminal_blocker_event_payload.v1",
        "terminal_blocker": dict(blocker),
        "next_legal_action": dict(blocker.get("next_legal_action") or {}),
        "next_legal_actions": list(blocker.get("next_legal_actions") or []),
        "route_identity": dict(route_identity),
        "task_identity": {
            "project_id": str(blocker.get("project_id") or ""),
            "backlog_id": str(blocker.get("backlog_id") or ""),
            "task_id": str(blocker.get("task_id") or ""),
            "attempt_num": int(blocker.get("attempt_num") or 0),
        },
        "branch_identity": {
            "branch": str(blocker.get("branch") or ""),
            "worktree": str(blocker.get("worktree") or ""),
            "fence_token": str(blocker.get("fence_token") or ""),
            "runtime_context_id": str(blocker.get("runtime_context_id") or ""),
            "base_commit": str(blocker.get("base_commit") or ""),
            "target_head_commit": str(blocker.get("target_head_commit") or ""),
            "merge_queue_id": str(blocker.get("merge_queue_id") or ""),
        },
        "timeout_no_progress": {
            "blocker_id": str(blocker.get("blocker_id") or ""),
            "reason": str(blocker.get("reason") or ""),
            "timeout_sec": blocker.get("timeout_sec", 0),
            "early_progress_timeout_sec": blocker.get("early_progress_timeout_sec", 0),
            "no_output": bool(blocker.get("no_output")),
            "no_finish_evidence": bool(blocker.get("no_finish_evidence")),
        },
        "command_projection": dict(blocker.get("terminal_contract_projection") or {}),
        "executable_worker_launch": dict(executable_worker_launch),
        "missing_launch_fields": list(blocker.get("missing_launch_fields") or []),
        "runtime_monitor_summary": dict(blocker.get("runtime_monitor_summary") or {}),
        "worktree_diff_scope": dict(diff_scope),
        "startup_read_receipt_recording_status": dict(startup_status),
        "runtime_context": dict(runtime_context),
        "service_router_suppress": True,
    }
    return {
        "task_id": str(blocker.get("task_id") or ""),
        "backlog_id": str(blocker.get("backlog_id") or ""),
        "attempt_num": int(blocker.get("attempt_num") or 0),
        "event_type": "observer_dogfood_terminal_blocker",
        "phase": "implementation_wait",
        "event_kind": "observer_cli_terminal_blocker",
        "correlation_id": str(blocker.get("observer_command_id") or ""),
        "severity": "error",
        "decision": "blocked",
        "schema_version": 2,
        "actor": "observer_dogfood",
        "status": "blocked",
        "payload": event_payload,
        "verification": {
            "schema_version": "observer_dogfood_terminal_blocker_verification.v1",
            "passed": False,
            "canonical_contract_state": "blocked",
            "command_projection_status": str(
                blocker.get("command_projection_status") or "failed"
            ),
            "startup_recorded": bool(startup_status.get("startup_recorded")),
            "read_receipt_recorded": bool(startup_status.get("read_receipt_recorded")),
            "implementation_evidence_recorded": False,
        },
        "artifact_refs": {
            "runtime_context_id": str(blocker.get("runtime_context_id") or ""),
            "worktree": str(blocker.get("worktree") or ""),
            "branch": str(blocker.get("branch") or ""),
            "fence_token": str(blocker.get("fence_token") or ""),
            "observer_command_id": str(blocker.get("observer_command_id") or ""),
            "blocker_id": str(blocker.get("blocker_id") or ""),
        },
        "commit_sha": str(diff_scope.get("head_commit") or ""),
    }


def _bounded_worker_no_progress_next_legal_action(
    *,
    runtime_context_id: str,
    task_id: str,
    parent_task_id: str,
    observer_command_id: str,
    worker_id: str,
    worker_agent_id: str,
    merge_queue_id: str,
    route_identity: Mapping[str, Any],
    missing_launch_fields: Sequence[str],
    reason: str,
) -> dict[str, Any]:
    missing = [str(item) for item in missing_launch_fields if str(item or "").strip()]
    primary = "repair_runtime_text_payload" if missing else "retry_with_new_worker"
    deterministic_order: list[str] = []
    for action_id in (
        primary,
        "retry_with_new_worker",
        "repair_runtime_text_payload",
        "authorize_explicit_hotfix_exception",
    ):
        if action_id not in deterministic_order:
            deterministic_order.append(action_id)
    return {
        "schema_version": BOUNDED_WORKER_NO_PROGRESS_NEXT_ACTION_SCHEMA_VERSION,
        "status": "blocked",
        "reason": reason,
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "observer_command_id": observer_command_id,
        "worker_id": worker_id,
        "worker_agent_id": worker_agent_id,
        "merge_queue_id": merge_queue_id,
        "route_identity": dict(route_identity),
        "missing_launch_fields": missing,
        "next_action": primary,
        "deterministic_order": deterministic_order,
        "actions": [
            {
                "id": "retry_with_new_worker",
                "action": "dispatch_bounded_worker",
                "reason": "start a fresh bounded worker lane for the same runtime context contract",
            },
            {
                "id": "repair_runtime_text_payload",
                "action": "observer_runtime_text_prepare",
                "reason": "repair missing launch or dispatch evidence before retrying the worker lane",
            },
            {
                "id": "authorize_explicit_hotfix_exception",
                "action": "authorize_observer_hotfix_exception",
                "reason": "only for an explicit audited operator exception",
            },
        ],
    }


def _append_dogfood_terminal_blocker_event(
    blocker: Mapping[str, Any],
) -> dict[str, Any]:
    event = _dogfood_terminal_blocker_timeline_event(blocker)
    project_id = str(blocker.get("project_id") or "")
    if not project_id:
        return {
            "ok": False,
            "event_type": event["event_type"],
            "event_kind": event["event_kind"],
            "phase": event["phase"],
            "request": event,
            "error": "missing_project_id",
        }
    try:
        recorded = _record_task_timeline_event(project_id=project_id, event=event)
    except Exception as exc:
        return {
            "ok": False,
            "event_type": event["event_type"],
            "event_kind": event["event_kind"],
            "phase": event["phase"],
            "request": event,
            "error": str(exc),
        }
    return {
        "ok": True,
        "event_type": event["event_type"],
        "event_kind": event["event_kind"],
        "phase": event["phase"],
        "event_id": recorded.get("id") or recorded.get("event_id") or "",
        "request": event,
        "event": recorded,
    }


def _materialize_worktree(
    *,
    main_worktree: Path,
    context: Any,
) -> dict[str, Any]:
    try:
        strategy = branch_strategy_from_runtime_context(
            context,
            repo_root_path=main_worktree,
        )
        worktree = batch_jobs.create_worktree(strategy, repo_root_path=main_worktree)
    except Exception as exc:
        return {
            "status": "failed",
            "materialized": False,
            "error": str(exc),
            "worktree": str(Path(str(getattr(context, "worktree_path", ""))).expanduser()),
        }
    status = _git_worktree_status(strategy.worktree_path, main_worktree=main_worktree)
    return {
        "status": "materialized" if status["is_git_worktree"] else "failed",
        "materialized": status["is_git_worktree"],
        "worktree": status["worktree"],
        "branch_strategy": strategy.to_metadata(),
        "worktree_result": worktree,
        "worktree_status": status,
    }


def _execution_gate_required(request: ObserverRunRequest) -> bool:
    return request.backend_mode in ONE_HOP_REQUIRED_BACKENDS


def _route_identity_mismatches(
    route: RoutePromptContract,
    gate: Mapping[str, Any],
) -> list[dict[str, str]]:
    checks = [
        ("route_context_hash", route.route_context_hash, str(gate.get("route_context_hash") or "")),
        ("prompt_contract_id", route.prompt_contract_id, str(gate.get("prompt_contract_id") or "")),
    ]
    expected_prompt_hash = str(route.prompt_contract_hash or "")
    actual_prompt_hash = str(gate.get("prompt_contract_hash") or "")
    if expected_prompt_hash or actual_prompt_hash:
        checks.append(("prompt_contract_hash", expected_prompt_hash, actual_prompt_hash))
    mismatches: list[dict[str, str]] = []
    for field, expected, actual in checks:
        if expected != actual:
            mismatches.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                }
            )
    return mismatches


def _dogfood_route_identity_validation(request: DogfoodObserverPlanRequest) -> dict[str, Any]:
    missing: list[str] = []
    if not str(request.route_id or "").strip():
        missing.append("route_id")
    if not str(request.visible_injection_manifest_hash or "").strip():
        missing.append("visible_injection_manifest_hash")
    return {
        "allowed": not missing,
        "missing": missing,
        "error": "observer dogfood requires complete route identity evidence"
        if missing
        else "",
        "route_id": request.route_id,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
    }


def validate_one_hop_execution_gate(request: ObserverRunRequest) -> dict[str, Any]:
    """Validate that a live observer/worker run is fenced to one hop.

    The lower-level MF gate already knows how to prove the isolated branch,
    worktree, fence token, merge queue, route context, and dirty-scope evidence.
    This observer gate adds the launcher-specific check that the invocation cwd
    matches the gated worktree.
    """

    if not _execution_gate_required(request):
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": False,
            "allowed": True,
            "reason": "backend_does_not_launch_code_mutating_cli",
        }

    if not request.dispatch_gate:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": ["dispatch_gate"],
            "error": "live observer execution requires one-hop dispatch gate evidence",
        }

    try:
        gate = validate_mf_subagent_dispatch_gate(
            request.dispatch_gate,
            target_worktree_path=request.main_worktree,
            main_worktree_path=request.main_worktree,
        )
    except MfSubagentContractError as exc:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": [],
            "error": str(exc),
        }

    route_mismatches = _route_identity_mismatches(request.route, gate)
    if route_mismatches:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": [],
            "error": "dispatch gate route identity does not match observer request",
            "route_identity_mismatches": route_mismatches,
            "dispatch_gate": gate,
        }

    workspace = _normalize_path(request.workspace or str(Path.cwd()))
    gated_worktree = _normalize_path(str(gate.get("worktree") or ""))
    if workspace != gated_worktree:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": [],
            "error": "observer workspace must match gated one-hop worktree",
            "workspace": workspace,
            "gated_worktree": gated_worktree,
            "dispatch_gate": gate,
        }

    worktree_status = _git_worktree_status(
        gated_worktree,
        main_worktree=request.main_worktree,
    )
    if not worktree_status["is_git_worktree"]:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": [],
            "error": "observer execution requires an isolated real git worker worktree",
            "worktree_status": worktree_status,
            "dispatch_gate": gate,
        }

    return {
        "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
        "required": True,
        "allowed": True,
        "dispatch_gate": gate,
        "worktree_status": worktree_status,
    }


def build_observer_prompt(request: ObserverRunRequest) -> str:
    if request.prompt:
        return request.prompt
    return (
        "You are the Aming Claw observer for a route-owned manual-fix run.\n"
        f"Project: {request.project_id}\n"
        f"Backlog: {request.backlog_id}\n"
        f"Route context hash: {request.route.route_context_hash}\n"
        f"Prompt contract id: {request.route.prompt_contract_id}\n\n"
        "Required order: acknowledge route context, query graph before file edits, "
        "execute only in the gated one-hop worktree, dispatch only bounded mf_sub "
        "workers through dispatch gates, record timeline "
        "evidence, and stop before merge/close unless verification gates pass. "
        "Actual startup evidence proves launch only; after startup, first progress "
        "must be graph/precheck/route progress, worktree/head changes, checkpoint, "
        "finish gate, or an explicit blocker. "
        "Do not expose raw private route/context-pack content."
    )


def build_observer_invocation_request(request: ObserverRunRequest) -> AIInvocationRequest:
    workspace = request.workspace or str(Path.cwd())
    metadata: dict[str, Any] = {
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "observer_launcher": True,
    }
    if request.early_progress_timeout_sec:
        metadata["early_progress_timeout_sec"] = float(request.early_progress_timeout_sec)
    if request.heartbeat_callback:
        metadata["heartbeat_callback"] = request.heartbeat_callback
        metadata["heartbeat_interval_sec"] = float(request.heartbeat_interval_sec or 10.0)
    return AIInvocationRequest(
        role="observer",
        provider=request.provider,
        model=request.model,
        backend_mode=request.backend_mode,
        cwd=workspace,
        prompt=build_observer_prompt(request),
        timeout_sec=request.timeout_sec,
        auth_mode="cli_auth" if request.backend_mode.endswith("_cli") else "api_key_env",
        route=request.route,
        metadata=metadata,
        env={str(key): str(value) for key, value in request.env.items()},
    )


def _runtime_text_items(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        text = values.strip()
        return [text] if text else []
    if isinstance(values, Mapping):
        return []
    return [str(item) for item in values if str(item or "").strip()]


def _runtime_text_nested_mappings(source: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(source, Mapping):
        return []
    mappings: list[Mapping[str, Any]] = []
    stack: list[Mapping[str, Any]] = [source]
    seen: set[int] = set()
    while stack:
        current = stack.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        mappings.append(current)
        for key in (
            "context",
            "branch_identity",
            "branch_context",
            "parallel_branch_runtime_context",
            "runtime_context",
            "runtime_contract",
            "runtime_context_projection",
            "runtime_context_current",
            "runtime_context_worker_view",
            "worker_view",
            "current",
            "current_state",
            "current_values",
            "gate_inputs",
            "close_gate_view",
            "payload",
            "contract",
            "worker_launch_pack",
            "startup_recording",
            "startup_gate",
            "mf_subagent_startup_gate",
            "read_receipt",
            "read_receipt_recording",
            "read_receipt_recording_append",
            "event",
        ):
            value = current.get(key)
            if isinstance(value, Mapping):
                stack.append(value)
        views = current.get("views")
        if isinstance(views, Mapping):
            for value in views.values():
                if isinstance(value, Mapping):
                    stack.append(value)
    return mappings


def _runtime_text_first_items_from_mappings(
    mappings: Sequence[Mapping[str, Any]],
    *field_names: str,
) -> list[str]:
    for source in mappings:
        for field_name in field_names:
            items = _runtime_text_items(source.get(field_name))  # type: ignore[arg-type]
            if items:
                return items
    return []


def _runtime_text_owned_file_scope(
    request: ObserverRuntimeTextPrepareRequest,
    *,
    branch_runtime_evidence: Mapping[str, Any] | None = None,
    supplied_projection: Mapping[str, Any] | None = None,
) -> list[str]:
    direct = _runtime_text_items(request.owned_files)
    if direct:
        return direct
    projection = supplied_projection if isinstance(supplied_projection, Mapping) else {}
    projected = _runtime_text_items(
        _runtime_text_projection_value(projection, "owned_files")
        or _runtime_text_projection_value(projection, "target_files")
    )
    if projected:
        return projected
    return _runtime_text_first_items_from_mappings(
        _runtime_text_nested_mappings(branch_runtime_evidence),
        "owned_files",
        "target_files",
    )


def _runtime_text_first_text_from_mappings(
    mappings: Sequence[Mapping[str, Any]],
    *field_names: str,
) -> tuple[str, str]:
    for source in mappings:
        for field_name in field_names:
            value = source.get(field_name)
            text = str(value or "").strip()
            if text:
                return text, field_name
    return "", ""


def _runtime_text_read_receipt_values(
    mappings: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    expanded_mappings: list[Mapping[str, Any]] = []
    seen: set[int] = set()

    def append_mapping(source: Mapping[str, Any]) -> None:
        marker = id(source)
        if marker in seen:
            return
        seen.add(marker)
        expanded_mappings.append(source)
        for key in (
            "read_receipt",
            "read_receipt_recording",
            "timeline_refs",
            "timeline_event_recorded",
            "current_values",
        ):
            value = source.get(key)
            if isinstance(value, Mapping):
                append_mapping(value)

    for mapping in mappings:
        append_mapping(mapping)
    mappings = expanded_mappings

    read_receipt_hash, hash_source = _runtime_text_first_text_from_mappings(
        mappings,
        "read_receipt_hash",
        "worker_read_receipt_hash",
        "startup_read_receipt_hash",
    )
    read_receipt_event_id, event_source = _runtime_text_first_text_from_mappings(
        mappings,
        "read_receipt_event_id",
        "read_receipt_timeline_event_id",
        "read_receipt_timeline_id",
        "read_receipt_event_ref",
        "startup_read_receipt_event_id",
    )
    if not read_receipt_hash:
        nested_hash, nested_hash_source = _runtime_text_first_text_from_mappings(
            mappings,
            "hash",
            "launch_text_hash",
        )
        if nested_hash and read_receipt_event_id:
            read_receipt_hash = nested_hash
            hash_source = nested_hash_source
    if not read_receipt_event_id:
        for source in mappings:
            looks_like_read_receipt = (
                str(source.get("event_kind") or "") == "mf_subagent_read_receipt"
                or str(source.get("schema_version") or "") == "mf_subagent_read_receipt.v1"
                or bool(
                    source.get("read_receipt_hash")
                    or source.get("worker_read_receipt_hash")
                    or source.get("launch_text_hash")
                )
            )
            if not looks_like_read_receipt:
                continue
            read_receipt_event_id, event_source = _runtime_text_first_text_from_mappings(
                [source],
                "timeline_event_id",
                "timeline_id",
                "event_id",
            )
            if read_receipt_event_id:
                break
    return {
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_event_id": read_receipt_event_id,
        "hash_source": hash_source,
        "event_id_source": event_source,
    }


def _runtime_text_read_receipt_identity(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    runtime_context_projection: Mapping[str, Any],
    branch_runtime_evidence: Mapping[str, Any],
    launch_text_hash: str,
) -> dict[str, Any]:
    supplied_mappings: list[Mapping[str, Any]] = []
    request_receipt = (
        request.read_receipt if isinstance(request.read_receipt, Mapping) else {}
    )
    if request.read_receipt_hash or request.read_receipt_event_id or request_receipt:
        supplied_mappings.append(
            {
                "source": "observer_runtime_text_prepare.request",
                "read_receipt_hash": request.read_receipt_hash,
                "read_receipt_event_id": request.read_receipt_event_id,
                "read_receipt": request_receipt,
            }
        )
    recorded_mappings: list[Mapping[str, Any]] = []
    recorded_mappings.extend(_runtime_text_projection_containers(runtime_context_projection))
    recorded_mappings.extend(_runtime_text_nested_mappings(branch_runtime_evidence))

    recorded_values = _runtime_text_read_receipt_values(recorded_mappings)
    supplied_values = _runtime_text_read_receipt_values(supplied_mappings)
    read_receipt_hash = recorded_values["read_receipt_hash"]
    read_receipt_event_id = recorded_values["read_receipt_event_id"]
    hash_source = recorded_values["hash_source"]
    event_source = recorded_values["event_id_source"]
    supplied_hash = supplied_values["read_receipt_hash"]
    supplied_event_id = supplied_values["read_receipt_event_id"]
    recorded = bool(read_receipt_hash and read_receipt_event_id)
    supplied_unverified = bool(supplied_hash or supplied_event_id or request_receipt)
    return {
        "schema_version": "observer_runtime_text_read_receipt_identity.v1",
        "status": (
            "recorded"
            if recorded
            else ("supplied_unverified" if supplied_unverified else "not_recorded")
        ),
        "recorded": recorded,
        "read_receipt_hash": read_receipt_hash if recorded else "",
        "read_receipt_event_id": read_receipt_event_id if recorded else "",
        "hash_source": hash_source if recorded else "",
        "event_id_source": event_source if recorded else "",
        "recorded_evidence_sources": [
            "runtime_context_projection",
            "hydrated_branch_runtime_evidence",
        ],
        "supplied_unverified": supplied_unverified,
        "supplied_read_receipt_hash": supplied_hash,
        "supplied_read_receipt_event_id": supplied_event_id,
        "supplied_read_receipt": dict(request_receipt),
        "supplied_source": (
            "observer_runtime_text_prepare.request" if supplied_unverified else ""
        ),
        "launch_text_hash": launch_text_hash,
        "accepted_hash_inputs": ["read_receipt_hash", "launch_text_hash"],
        "next_legal_action": (
            "record_mf_subagent_startup"
            if recorded
            else "submit_mf_subagent_read_receipt"
        ),
        "recording_rule": (
            "Do not fabricate read_receipt_event_id. If no durable "
            "mf_subagent_read_receipt timeline event exists, record that event "
            "before startup. Request-supplied read receipt fields are repair "
            "hints only and never count as recorded timeline evidence."
        ),
    }


def _runtime_text_observer_command_id(
    request: ObserverRuntimeTextPrepareRequest,
    *,
    branch_runtime_evidence: Mapping[str, Any] | None = None,
) -> str:
    evidence = branch_runtime_evidence if isinstance(branch_runtime_evidence, Mapping) else {}
    evidence_context = (
        evidence.get("context") if isinstance(evidence.get("context"), Mapping) else {}
    )
    supplied_projection = (
        request.runtime_context_projection
        if isinstance(request.runtime_context_projection, Mapping)
        else {}
    )
    return str(
        _first_non_empty(
            request.observer_command_id,
            _runtime_text_projection_value(supplied_projection, "observer_command_id"),
            evidence.get("observer_command_id"),
            evidence_context.get("observer_command_id"),
        )
        or ""
    ).strip()


def _runtime_text_observer_command_requirement(
    *,
    observer_command_id: str,
    backlog_id: str,
    route_context_hash: str,
    prompt_contract_id: str,
) -> dict[str, Any]:
    missing = not bool(observer_command_id)
    return {
        "schema_version": "observer_runtime_text_observer_command_requirement.v1",
        "required": True,
        "status": "present" if not missing else "missing",
        "observer_command_id": observer_command_id,
        "required_command_type": "execute_backlog_row",
        "backlog_id": backlog_id,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "claim_rule": (
            "Use the claimed backlog-specific execute_backlog_row command id "
            "for this backlog and route identity."
        ),
        "forbidden_substitution": (
            "Do not use a blind observer_command_next result as startup lineage "
            "unless it is the exact claimed execute_backlog_row command for this "
            "backlog and route identity."
        ),
        "blocker_id": (
            "missing_execute_backlog_row_observer_command_id" if missing else ""
        ),
    }


def _runtime_text_parent_route_lineage(
    request: ObserverRuntimeTextPrepareRequest,
) -> dict[str, Any]:
    parent_identity = (
        request.parent_route_identity
        if isinstance(request.parent_route_identity, Mapping)
        else {}
    )
    route_id = str(parent_identity.get("route_id") or request.route_id or "").strip()
    route_context_hash = str(
        parent_identity.get("route_context_hash") or request.route.route_context_hash
    ).strip()
    prompt_contract_id = str(
        parent_identity.get("prompt_contract_id") or request.route.prompt_contract_id
    ).strip()
    prompt_contract_hash = str(
        parent_identity.get("prompt_contract_hash") or request.route.prompt_contract_hash
    ).strip()
    route_token_ref = str(
        parent_identity.get("route_token_ref") or request.route.route_token_ref
    ).strip()
    visible_manifest = str(
        parent_identity.get("visible_injection_manifest_hash")
        or request.visible_injection_manifest_hash
    ).strip()
    return {
        "schema_version": "parent_route_lineage.v1",
        "route_id": route_id,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "route_token_ref": route_token_ref,
        "visible_injection_manifest_hash": visible_manifest,
        "selected_project": request.project_id,
        "selected_backlog_id": request.backlog_id,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "allowed_actions": [
            "prepare_runtime_text",
            "dispatch_bounded_worker",
            "task_timeline_append",
            "run_tests",
            "git_diff",
            "report_review_ready",
        ],
        "blocked_actions": [
            "merge",
            "push",
            "activate_graph",
            "release_gate",
            "create_task",
            "delete_worktree",
            "modify_merge_queue",
            "expose_raw_private_route_context",
        ],
        "required_lanes": list(RUNTIME_TEXT_REQUIRED_LANES),
        "required_evidence": list(RUNTIME_TEXT_REQUIRED_EVIDENCE),
    }


def _runtime_text_graph_trace_evidence(
    *,
    graph_trace_ids: Sequence[str],
    task_id: str,
    parent_task_id: str,
    fence_token: str,
) -> dict[str, Any]:
    trace_ids = _runtime_text_items(graph_trace_ids)
    return {
        "schema_version": "mf_subagent_graph_trace.v1",
        "query_source": "mf_subagent",
        "query_purpose": "subagent_context_build",
        "trace_ids": trace_ids,
        "trace_count": len(trace_ids),
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "fence_token": fence_token,
    }


def _runtime_text_prelaunch_graph_context(
    *, graph_trace_ids: Sequence[str]
) -> dict[str, Any]:
    trace_ids = _runtime_text_items(graph_trace_ids)
    return {
        "schema_version": "observer_prelaunch_graph_context.v1",
        "trace_ids": trace_ids,
        "trace_count": len(trace_ids),
        "counts_as_worker_graph_trace_evidence": False,
        "message": (
            "Prelaunch/observer graph context is dispatch context only. "
            "Finish gates require worker-owned mf_subagent graph trace evidence "
            "recorded after startup/read receipt."
        ),
    }


def _runtime_text_branch_runtime_evidence(
    *,
    project_id: str,
    context: Any,
    parent_task_id: str,
    branch_runtime_registration_ref: str = "",
    branch_runtime_evidence: Mapping[str, Any] | None = None,
    runtime_context_id: str = "",
) -> dict[str, Any]:
    supplied = dict(branch_runtime_evidence or {})
    nested = supplied.get("branch_runtime_evidence")
    if isinstance(nested, Mapping):
        supplied = {**supplied, **dict(nested)}
    supplied_registration_ref = str(
        supplied.get("registration_ref")
        or supplied.get("source_ref")
        or supplied.get("allocation_source_ref")
        or supplied.get("api_ref")
        or supplied.get("api_route")
        or ""
    ).strip()
    requested_registration_ref = str(branch_runtime_registration_ref or "").strip()
    registration_ref = (
        supplied_registration_ref
        if requested_registration_ref.startswith("mfrctx-")
        else (requested_registration_ref or supplied_registration_ref)
    )
    planned_context = {
        "runtime_context_id": runtime_context_id_for_branch_context(context),
        "governance_project_id": context.governance_project_id or project_id,
        "target_project_id": context.target_project_id or project_id,
        "target_project_root": context.target_project_root,
        "allocation_owner": context.allocation_owner or context.agent_id,
        "observer_allocation_owner": context.allocation_owner or context.agent_id,
        "worker_slot_id": context.worker_slot_id or context.worker_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "fence_token": context.fence_token,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
    }

    def _nested_mappings(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        mappings: list[Mapping[str, Any]] = [source]
        for key in (
            "context",
            "branch_identity",
            "branch_context",
            "parallel_branch_runtime_context",
        ):
            value = source.get(key)
            if isinstance(value, Mapping):
                mappings.append(value)
        return mappings

    def _evidence_field(*keys: str) -> str:
        for source in _nested_mappings(supplied):
            for key in keys:
                raw_value = source.get(key)
                if isinstance(raw_value, (Mapping, list, tuple)):
                    continue
                value = str(raw_value or "").strip()
                if value:
                    return value
        return ""

    def _evidence_parent_task_id() -> str:
        nested_sources = _nested_mappings(supplied)
        for source in nested_sources[1:]:
            value = str(source.get("parent_task_id") or "").strip()
            if value:
                return value
        for source in nested_sources[1:]:
            value = _runtime_text_context_parent(source)
            if value:
                return value
        for source in nested_sources[:1]:
            value = str(source.get("parent_task_id") or "").strip()
            if value:
                return value
        for source in nested_sources[:1]:
            value = _runtime_text_context_parent(source)
            if value:
                return value
        return ""

    supplied_runtime_context_id = str(
        runtime_context_id
        or (branch_runtime_registration_ref if branch_runtime_registration_ref.startswith("mfrctx-") else "")
        or ""
    ).strip()
    evidence_runtime_context_id = _evidence_field("runtime_context_id")
    registration_ref_valid = bool(
        registration_ref
        and any(marker in registration_ref for marker in RUNTIME_TEXT_BRANCH_RUNTIME_REF_MARKERS)
    )
    supplied_status = str(supplied.get("status") or "").strip().lower()
    supplied_rejected = bool(supplied) and (
        supplied.get("ok") is False
        or supplied.get("registered") is False
        or bool(supplied.get("allocation_required"))
        or supplied_status in {"allocation_required", "rejected", "failed", "error"}
    )
    if supplied_rejected:
        return {
            "schema_version": "mf_subagent_branch_runtime.v1",
            "status": "allocation_required",
            "allocation_required": True,
            "registered": False,
            "present": bool(supplied),
            "source": "observer_runtime_text_prepare",
            "message": str(
                supplied.get("message")
                or "Supplied branch runtime evidence is not a registered allocation."
            ),
            "runtime_context_id": evidence_runtime_context_id or supplied_runtime_context_id,
            "supplied_source_ref": registration_ref,
            "supplied_evidence_status": supplied_status or supplied.get("status") or "",
            "supplied_message": str(supplied.get("message") or ""),
            "missing_fields": (
                list(supplied.get("missing_fields"))
                if isinstance(supplied.get("missing_fields"), list)
                else []
            ),
            "mismatches": (
                list(supplied.get("mismatches"))
                if isinstance(supplied.get("mismatches"), list)
                else []
            ),
            "planned_context": planned_context,
        }
    if not registration_ref_valid:
        message = (
            "Bare runtime_context_id must resolve through persisted branch runtime "
            "allocation evidence before observer runtime text can be dispatch-ready."
            if supplied_runtime_context_id.startswith("mfrctx-")
            or evidence_runtime_context_id.startswith("mfrctx-")
            else (
                "Call MCP parallel_branch_allocate, "
                f"POST /api/graph-governance/{project_id}/parallel-branches/allocate, "
                "or upsert_branch_context, then pass branch_runtime_evidence with "
                "a valid allocation source ref and runtime_context_id."
            )
        )
        return {
            "schema_version": "mf_subagent_branch_runtime.v1",
            "status": "allocation_required",
            "allocation_required": True,
            "registered": False,
            "present": bool(supplied),
            "source": "observer_runtime_text_prepare",
            "message": message,
            "runtime_context_id": evidence_runtime_context_id or supplied_runtime_context_id,
            "supplied_source_ref": registration_ref,
            "planned_context": planned_context,
        }

    observed_context = {
        "runtime_context_id": evidence_runtime_context_id,
        "governance_project_id": _evidence_field("governance_project_id"),
        "target_project_id": _evidence_field("target_project_id"),
        "target_project_root": _evidence_field("target_project_root"),
        "allocation_owner": _evidence_field(
            "allocation_owner",
            "observer_allocation_owner",
        ),
        "observer_allocation_owner": _evidence_field(
            "observer_allocation_owner",
            "allocation_owner",
        ),
        "worker_slot_id": _evidence_field("worker_slot_id", "worker_id"),
        "task_id": _evidence_field("task_id"),
        "parent_task_id": _evidence_parent_task_id(),
        "branch_ref": _evidence_field("branch_ref", "branch"),
        "ref_name": _evidence_field("ref_name"),
        "worktree_id": _evidence_field("worktree_id"),
        "fence_token": _evidence_field("fence_token"),
        "worktree_path": _evidence_field("worktree_path", "worktree"),
        "base_commit": _evidence_field("base_commit"),
        "target_head_commit": _evidence_field("target_head_commit"),
        "merge_queue_id": _evidence_field("merge_queue_id"),
    }
    required_fields = {
        "task_id": context.task_id,
        "fence_token": context.fence_token,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
    }
    if parent_task_id:
        required_fields["parent_task_id"] = parent_task_id
    missing = [
        field
        for field, expected in required_fields.items()
        if expected and not observed_context.get(field)
    ]
    if not observed_context["runtime_context_id"]:
        missing.append("runtime_context_id")
    mismatches = [
        field
        for field, expected in required_fields.items()
        if expected
        and observed_context.get(field)
        and observed_context[field] != expected
    ]
    if (
        supplied_runtime_context_id
        and observed_context["runtime_context_id"]
        and observed_context["runtime_context_id"] != supplied_runtime_context_id
    ):
        mismatches.append("runtime_context_id")
    if missing or mismatches:
        return {
            "schema_version": "mf_subagent_branch_runtime.v1",
            "status": "allocation_required",
            "allocation_required": True,
            "registered": False,
            "present": bool(supplied),
            "source": "observer_runtime_text_prepare",
            "message": "Supplied branch runtime evidence is missing persisted context identity.",
            "runtime_context_id": evidence_runtime_context_id or supplied_runtime_context_id,
            "supplied_source_ref": registration_ref,
            "missing_fields": sorted(set(missing)),
            "mismatch_fields": sorted(set(mismatches)),
            "planned_context": planned_context,
            "observed_context": observed_context,
        }
    return {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "status": supplied.get("status") or "registered",
        "ok": True,
        "present": True,
        "source_ref": registration_ref,
        "registration_ref": registration_ref,
        "runtime_context_id": observed_context["runtime_context_id"],
        "registration_source": str(
            supplied.get("registration_source") or "caller_supplied_allocation_evidence"
        ),
        "allocation_required": False,
        "registered": True,
        "task_id": observed_context["task_id"],
        "parent_task_id": observed_context["parent_task_id"],
        "fence_token": observed_context["fence_token"],
        "branch_ref": observed_context["branch_ref"],
        "ref_name": observed_context["ref_name"],
        "worktree_id": observed_context["worktree_id"],
        "worktree_path": observed_context["worktree_path"],
        "target_project_root": (
            observed_context["target_project_root"]
            or planned_context["target_project_root"]
        ),
        "base_commit": observed_context["base_commit"],
        "target_head_commit": observed_context["target_head_commit"],
        "merge_queue_id": observed_context["merge_queue_id"],
        "context": {
            "runtime_context_id": observed_context["runtime_context_id"],
            "governance_project_id": observed_context["governance_project_id"]
            or planned_context["governance_project_id"],
            "target_project_id": observed_context["target_project_id"]
            or planned_context["target_project_id"],
            "target_project_root": observed_context["target_project_root"]
            or planned_context["target_project_root"],
            "allocation_owner": observed_context["allocation_owner"]
            or planned_context["allocation_owner"],
            "observer_allocation_owner": observed_context["observer_allocation_owner"]
            or planned_context["observer_allocation_owner"],
            "worker_slot_id": observed_context["worker_slot_id"]
            or planned_context["worker_slot_id"],
            "task_id": observed_context["task_id"],
            "parent_task_id": observed_context["parent_task_id"],
            "fence_token": observed_context["fence_token"],
            "branch_ref": observed_context["branch_ref"],
            "ref_name": observed_context["ref_name"],
            "worktree_id": observed_context["worktree_id"],
            "worktree_path": observed_context["worktree_path"],
            "base_commit": observed_context["base_commit"],
            "target_head_commit": observed_context["target_head_commit"],
            "merge_queue_id": observed_context["merge_queue_id"],
        },
    }


def _runtime_text_branch_runtime_packet(
    branch_runtime_registration_ref: str,
    branch_runtime_evidence: Mapping[str, Any] | None,
    runtime_context_id: str = "",
) -> dict[str, Any]:
    packet = dict(branch_runtime_evidence or {})
    nested = packet.get("branch_runtime_evidence")
    if isinstance(nested, Mapping):
        packet = {**packet, **dict(nested)}
    runtime_id = str(runtime_context_id or "").strip()
    ref = str(branch_runtime_registration_ref or "").strip()
    if runtime_id:
        packet.setdefault("runtime_context_id", runtime_id)
    if ref.startswith("mfrctx-"):
        packet.setdefault("runtime_context_id", ref)
    elif ref:
        packet.setdefault("registration_ref", ref)
    return packet


def _runtime_text_packet_context(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    context = packet.get("context")
    return context if isinstance(context, Mapping) else {}


def _runtime_text_packet_registered(packet: Mapping[str, Any]) -> bool:
    context = _runtime_text_packet_context(packet)
    return bool(
        packet
        and packet.get("registered") is not False
        and not packet.get("allocation_required")
        and str(packet.get("status") or "").strip().lower()
        not in {"allocation_required", "rejected", "failed", "error"}
        and str(packet.get("runtime_context_id") or context.get("runtime_context_id") or "").strip()
        and context
    )


def _runtime_text_allocation_required_evidence(
    *,
    runtime_context_id: str = "",
    supplied_source_ref: str = "",
    message: str,
    missing_fields: Sequence[str] | None = None,
    mismatches: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "status": "allocation_required",
        "ok": False,
        "present": False,
        "registered": False,
        "allocation_required": True,
        "runtime_context_id": runtime_context_id,
        "supplied_source_ref": supplied_source_ref,
        "source": "observer_runtime_text_prepare",
        "message": message,
        "missing_fields": list(missing_fields or []),
        "mismatches": [dict(item) for item in (mismatches or [])],
    }


def _runtime_text_governance_url() -> str:
    return os.getenv("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")


def _runtime_text_contract_service_query(
    *,
    runtime_context_id: str,
    task_id: str = "",
    parent_task_id: str = "",
    fence_token: str = "",
) -> dict[str, str]:
    query = {
        "runtime_context_id": runtime_context_id,
        "worker_role": "mf_sub",
    }
    if task_id:
        query["task_id"] = task_id
    if parent_task_id:
        query["parent_task_id"] = parent_task_id
    if fence_token:
        query["fence_token"] = fence_token
    return query


def _runtime_text_contract_service_url(
    *,
    project_id: str,
    runtime_context_id: str,
    query: Mapping[str, str],
) -> str:
    base_url = _runtime_text_governance_url()
    quoted_project = urllib.parse.quote(project_id, safe="")
    quoted_runtime = urllib.parse.quote(runtime_context_id, safe="")
    encoded_query = urllib.parse.urlencode(dict(query))
    return (
        f"{base_url}/api/graph-governance/{quoted_project}/parallel-branches/"
        f"runtime-contexts/{quoted_runtime}/runtime-contract?{encoded_query}"
    )


def _runtime_text_runtime_contract_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = payload.get("runtime_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    context = contract.get("runtime_context")
    return context if isinstance(context, Mapping) else {}


def _runtime_text_branch_evidence_from_runtime_contract(
    *,
    project_id: str,
    runtime_context_id: str,
    payload: Mapping[str, Any],
    source_ref: str,
) -> dict[str, Any]:
    context = dict(_runtime_text_runtime_contract_context(payload))
    context_runtime_id = str(context.get("runtime_context_id") or "").strip()
    if not context or context_runtime_id != runtime_context_id:
        return {}
    context.setdefault("project_id", project_id)
    context.setdefault(
        "governance_project_id",
        payload.get("governance_project_id") or project_id,
    )
    context.setdefault("target_project_id", payload.get("target_project_id") or project_id)
    context.setdefault(
        "allocation_owner",
        context.get("observer_allocation_owner") or context.get("agent_id") or "",
    )
    context.setdefault("observer_allocation_owner", context.get("allocation_owner") or "")
    context.setdefault("worker_slot_id", context.get("worker_id") or "")
    return {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "status": context.get("status") or "registered",
        "ok": True,
        "present": True,
        "registered": True,
        "allocation_required": False,
        "source_ref": source_ref,
        "registration_ref": source_ref,
        "allocation_source_ref": source_ref,
        "registration_source": "runtime_contract_service",
        "runtime_context_id": runtime_context_id,
        "context": context,
    }


def _runtime_text_get_service_branch_runtime_evidence(
    *,
    project_id: str,
    runtime_context_id: str,
    task_id: str = "",
    parent_task_id: str = "",
    fence_token: str = "",
) -> dict[str, Any]:
    runtime_id = str(runtime_context_id or "").strip()
    if not runtime_id:
        return {}
    query = _runtime_text_contract_service_query(
        runtime_context_id=runtime_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        fence_token=fence_token,
    )
    url = _runtime_text_contract_service_url(
        project_id=project_id,
        runtime_context_id=runtime_id,
        query=query,
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local governance URL
            body = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, TimeoutError):
        return {}
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        return {}
    return _runtime_text_branch_evidence_from_runtime_contract(
        project_id=project_id,
        runtime_context_id=runtime_id,
        payload=payload,
        source_ref=url,
    )


def _runtime_text_runtime_context_id_from_packet(
    *,
    branch_runtime_registration_ref: str,
    packet: Mapping[str, Any],
    runtime_context_id: str = "",
) -> str:
    direct = str(runtime_context_id or packet.get("runtime_context_id") or "").strip()
    if direct:
        return direct
    context = _runtime_text_packet_context(packet)
    direct = str(context.get("runtime_context_id") or "").strip()
    if direct:
        return direct
    ref = str(branch_runtime_registration_ref or packet.get("registration_ref") or "").strip()
    return ref if ref.startswith("mfrctx-") else ""


def _runtime_text_task_id_from_packet(
    *,
    task_id: str,
    packet: Mapping[str, Any],
) -> str:
    direct = str(task_id or packet.get("task_id") or "").strip()
    if direct:
        return direct
    context = _runtime_text_packet_context(packet)
    return str(context.get("task_id") or "").strip()


def _runtime_text_get_persisted_branch_context(
    *,
    project_id: str,
    runtime_context_id: str = "",
    task_id: str = "",
) -> Any:
    try:
        from governance.db import get_connection
        from governance.parallel_branch_runtime import (
            get_branch_context,
            get_branch_context_by_runtime_context_id,
        )
    except ImportError:  # pragma: no cover - package import path
        from agent.governance.db import get_connection
        from agent.governance.parallel_branch_runtime import (
            get_branch_context,
            get_branch_context_by_runtime_context_id,
        )

    conn = get_connection(project_id)
    try:
        context = None
        if runtime_context_id:
            context = get_branch_context_by_runtime_context_id(
                conn,
                project_id,
                runtime_context_id,
            )
        if context is None and task_id:
            context = get_branch_context(conn, project_id, task_id)
        return context
    finally:
        conn.close()


def _runtime_text_parent_candidates_from_branch_context(context: Any) -> list[str]:
    values = [
        str(getattr(context, "parent_task_id", "") or "").strip(),
        str(getattr(context, "chain_id", "") or "").strip(),
        str(getattr(context, "root_task_id", "") or "").strip(),
        str(getattr(context, "stage_task_id", "") or "").strip(),
        str(getattr(context, "backlog_id", "") or "").strip(),
    ]
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _runtime_text_parent_from_branch_context(context: Any) -> str:
    candidates = _runtime_text_parent_candidates_from_branch_context(context)
    root_task_id = str(getattr(context, "root_task_id", "") or "").strip()
    task_id = str(getattr(context, "task_id", "") or "").strip()
    for candidate in candidates:
        if (
            candidate
            and candidate not in {root_task_id, task_id}
            and not candidate.startswith(("chain-", "cchain-"))
        ):
            return candidate
    return candidates[0] if candidates else ""


def _runtime_text_expected_field_mismatches(
    context: Any,
    *,
    expected_fields: Mapping[str, str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    expected_fields = dict(expected_fields or {})
    parent_candidates = _runtime_text_parent_candidates_from_branch_context(context)
    actual = {
        "task_id": str(getattr(context, "task_id", "") or ""),
        "parent_task_id": _runtime_text_parent_from_branch_context(context),
        "fence_token": str(getattr(context, "fence_token", "") or ""),
        "worktree_path": str(getattr(context, "worktree_path", "") or ""),
        "base_commit": str(getattr(context, "base_commit", "") or ""),
        "target_head_commit": str(getattr(context, "target_head_commit", "") or ""),
        "merge_queue_id": str(getattr(context, "merge_queue_id", "") or ""),
    }
    missing: list[str] = []
    mismatches: list[dict[str, str]] = []
    for field, expected in expected_fields.items():
        expected_text = str(expected or "").strip()
        if not expected_text:
            continue
        if field == "parent_task_id":
            if not parent_candidates:
                missing.append(field)
            elif expected_text not in parent_candidates:
                mismatches.append(
                    {
                        "field": field,
                        "expected": expected_text,
                        "actual": actual[field],
                    }
                )
            continue
        if not actual.get(field):
            missing.append(field)
        elif actual[field] != expected_text:
            mismatches.append(
                {"field": field, "expected": expected_text, "actual": actual[field]}
            )
    return missing, mismatches


def _runtime_text_hydrate_persisted_branch_runtime_evidence(
    *,
    project_id: str,
    task_id: str = "",
    branch_runtime_registration_ref: str = "",
    branch_runtime_evidence: Mapping[str, Any] | None = None,
    runtime_context_id: str = "",
    expected_fields: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    packet = _runtime_text_branch_runtime_packet(
        branch_runtime_registration_ref,
        branch_runtime_evidence,
        runtime_context_id,
    )
    if packet.get("allocation_required"):
        return packet
    if _runtime_text_packet_registered(packet):
        return packet

    expected = dict(expected_fields or {})
    lookup_runtime_context_id = _runtime_text_runtime_context_id_from_packet(
        branch_runtime_registration_ref=branch_runtime_registration_ref,
        packet=packet,
        runtime_context_id=runtime_context_id,
    )
    lookup_task_id = _runtime_text_task_id_from_packet(task_id=task_id, packet=packet)
    source_ref = str(
        packet.get("registration_ref")
        or packet.get("source_ref")
        or packet.get("allocation_source_ref")
        or branch_runtime_registration_ref
        or ""
    ).strip()
    marker_only_registration_ref = bool(
        source_ref
        and any(marker in source_ref for marker in RUNTIME_TEXT_BRANCH_RUNTIME_REF_MARKERS)
        and not lookup_runtime_context_id
        and not _runtime_text_packet_context(packet)
    )
    if marker_only_registration_ref:
        missing_fields = ["runtime_context_id", "worktree_path"]
        missing_fields.extend(
            field for field, value in expected.items() if str(value or "").strip()
        )
        return _runtime_text_allocation_required_evidence(
            supplied_source_ref=source_ref,
            message=(
                "Branch runtime registration ref is only an allocation marker; "
                "pass persisted branch_runtime_evidence with runtime_context_id."
            ),
            missing_fields=sorted(set(missing_fields)),
        )
    if not lookup_runtime_context_id and not lookup_task_id:
        return packet

    if lookup_runtime_context_id:
        service_packet = _runtime_text_get_service_branch_runtime_evidence(
            project_id=project_id,
            runtime_context_id=lookup_runtime_context_id,
            task_id=lookup_task_id,
            parent_task_id=str(expected.get("parent_task_id") or ""),
            fence_token=str(expected.get("fence_token") or ""),
        )
        if service_packet:
            return service_packet

    try:
        context = _runtime_text_get_persisted_branch_context(
            project_id=project_id,
            runtime_context_id=lookup_runtime_context_id,
            task_id=lookup_task_id,
        )
    except Exception as exc:
        if not lookup_runtime_context_id:
            return packet
        return _runtime_text_allocation_required_evidence(
            runtime_context_id=lookup_runtime_context_id,
            supplied_source_ref=source_ref,
            message=f"Persisted branch runtime allocation lookup failed: {exc}",
        )
    if context is None:
        if not lookup_runtime_context_id:
            return packet
        return _runtime_text_allocation_required_evidence(
            runtime_context_id=lookup_runtime_context_id,
            supplied_source_ref=source_ref,
            message="Persisted branch runtime allocation was not found.",
        )

    if lookup_runtime_context_id:
        actual_runtime_context_id = runtime_context_id_for_branch_context(context)
        if actual_runtime_context_id != lookup_runtime_context_id:
            return _runtime_text_allocation_required_evidence(
                runtime_context_id=lookup_runtime_context_id,
                supplied_source_ref=source_ref,
                message="Persisted branch runtime allocation identity mismatch.",
                mismatches=[
                    {
                        "field": "runtime_context_id",
                        "expected": lookup_runtime_context_id,
                        "actual": actual_runtime_context_id,
                    }
                ],
            )

    missing, mismatches = _runtime_text_expected_field_mismatches(
        context,
        expected_fields=expected,
    )
    if missing or mismatches:
        return _runtime_text_allocation_required_evidence(
            runtime_context_id=runtime_context_id_for_branch_context(context),
            supplied_source_ref=source_ref,
            message="Persisted branch runtime allocation identity mismatch.",
            missing_fields=missing,
            mismatches=mismatches,
        )

    try:
        from governance.parallel_branch_runtime import branch_runtime_allocation_evidence
    except ImportError:  # pragma: no cover - package import path
        from agent.governance.parallel_branch_runtime import branch_runtime_allocation_evidence

    return branch_runtime_allocation_evidence(
        context,
        source_ref=source_ref
        or f"/api/graph-governance/{project_id}/parallel-branches/allocate",
        registration_source="persisted_branch_runtime_context",
    )


def _runtime_text_context_parent(context: Mapping[str, Any]) -> str:
    root_task_id = str(context.get("root_task_id") or "").strip()
    task_id = str(context.get("task_id") or "").strip()
    parent_task_id = str(context.get("parent_task_id") or "").strip()
    if (
        parent_task_id
        and parent_task_id != task_id
        and not parent_task_id.startswith(("chain-", "cchain-"))
    ):
        return parent_task_id
    candidates = [
        str(context.get("chain_id") or "").strip(),
        root_task_id,
        str(context.get("stage_task_id") or "").strip(),
        str(context.get("backlog_id") or "").strip(),
    ]
    for candidate in candidates:
        if (
            candidate
            and candidate not in {root_task_id, task_id}
            and not candidate.startswith(("chain-", "cchain-"))
        ):
            return candidate
    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def _runtime_text_apply_branch_runtime_context(
    context: Any,
    *,
    parent_task_id: str,
    branch_runtime_registration_ref: str = "",
    branch_runtime_evidence: Mapping[str, Any] | None = None,
    runtime_context_id: str = "",
    explicit_expected_fields: Mapping[str, str] | None = None,
) -> Any:
    packet = _runtime_text_branch_runtime_packet(
        branch_runtime_registration_ref,
        branch_runtime_evidence,
        runtime_context_id,
    )
    source_ref = str(
        packet.get("registration_ref")
        or packet.get("source_ref")
        or packet.get("allocation_source_ref")
        or packet.get("api_ref")
        or packet.get("api_route")
        or ""
    ).strip()
    if not any(marker in source_ref for marker in RUNTIME_TEXT_BRANCH_RUNTIME_REF_MARKERS):
        return context
    supplied_context = packet.get("context")
    if not isinstance(supplied_context, Mapping):
        return context
    explicit_expected_fields = dict(explicit_expected_fields or {})
    checks = {
        "task_id": (
            explicit_expected_fields.get("task_id") or context.task_id,
            str(supplied_context.get("task_id") or ""),
        ),
        "parent_task_id": (
            explicit_expected_fields.get("parent_task_id") or parent_task_id,
            _runtime_text_context_parent(supplied_context),
        ),
        "fence_token": (
            explicit_expected_fields.get("fence_token") or "",
            str(supplied_context.get("fence_token") or ""),
        ),
        "base_commit": (
            explicit_expected_fields.get("base_commit") or "",
            str(supplied_context.get("base_commit") or ""),
        ),
        "target_head_commit": (
            explicit_expected_fields.get("target_head_commit") or "",
            str(supplied_context.get("target_head_commit") or ""),
        ),
        "merge_queue_id": (
            explicit_expected_fields.get("merge_queue_id") or "",
            str(supplied_context.get("merge_queue_id") or ""),
        ),
    }
    if any(expected and actual and expected != actual for expected, actual in checks.values()):
        return context

    replacements: dict[str, Any] = {}
    for field_name in (
        "runtime_context_id",
        "batch_id",
        "backlog_id",
        "parent_task_id",
        "chain_id",
        "root_task_id",
        "stage_task_id",
        "stage_type",
        "branch_ref",
        "ref_name",
        "worktree_id",
        "worktree_path",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "fence_token",
        "worker_id",
        "worker_slot_id",
        "agent_id",
        "allocation_owner",
        "actual_host_worker_id",
        "host_startup_id",
        "host_session_id",
        "governance_project_id",
        "target_project_id",
        "target_project_root",
        "target_files",
        "owned_files",
        "status",
    ):
        if field_name == "runtime_context_id":
            value = packet.get("runtime_context_id") or supplied_context.get("runtime_context_id")
        else:
            value = supplied_context.get(field_name)
        if field_name in {"target_files", "owned_files"}:
            values = _runtime_text_items(value)
            if values:
                replacements[field_name] = tuple(values)
            continue
        if value not in (None, ""):
            replacements[field_name] = value
    return replace(context, **replacements) if replacements else context


def _runtime_text_service_dispatch_evidence() -> dict[str, Any]:
    return {
        "schema_version": "observer_subagent_service_dispatch.v1",
        "documented_host_adapter_boundary": "observer_runtime_text_service.v1",
        "host_adapter_boundary": (
            "Host-created Codex mf_sub worker receives runtime launch text; "
            "this service does not invoke ServiceManager or an executor worker."
        ),
        "boundary_documentation_ref": (
            "docs/governance/manual-fix-sop.md#observer-runtime-text-service"
        ),
        "documented": True,
    }


def _runtime_text_startup_echo_contract(
    *,
    runtime_context_id: str,
    observer_command_id: str,
    context: Any,
    parent_task_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "mf_subagent_startup_echo.v1",
        "required": True,
        "runtime_context_id": runtime_context_id,
        "must_echo_fields": [
            "runtime_context_id",
            "observer_command_id",
            "project_id",
            "task_id",
            "parent_task_id",
            "worker_role",
            "worker_slot_id",
            "actual_host_worker_id",
            "fence_token",
            "actual_cwd",
            "actual_git_root",
            "branch",
            "head_commit",
        ],
        "expected": {
            "project_id": context.project_id,
            "observer_command_id": observer_command_id,
            "governance_project_id": context.governance_project_id
            or context.project_id,
            "target_project_id": context.target_project_id or context.project_id,
            "target_project_root": context.target_project_root,
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "allocation_owner": context.allocation_owner or context.agent_id,
            "observer_allocation_owner": context.allocation_owner or context.agent_id,
            "worker_slot_id": context.worker_slot_id or context.worker_id,
            "fence_token": context.fence_token,
            "worktree_path": context.worktree_path,
            "branch_ref": context.branch_ref,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
        },
    }


def _runtime_text_startup_intent_event(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    runtime_context_id: str,
    observer_command_id: str,
    context: Any,
    parent_task_id: str,
    launch_text_hash: str,
    graph_trace_ids: Sequence[str],
) -> dict[str, Any]:
    head_commit = context.head_commit or context.target_head_commit
    startup_intent = {
        "schema_version": "mf_subagent_startup_intent.v1",
        "intent_kind": "mf_subagent.startup_intent",
        "status": "planned",
        "close_satisfying": False,
        "actual_startup_required": True,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "launch_text_hash": launch_text_hash,
        "raw_launch_text_persisted": False,
        "project_id": request.project_id,
        "governance_project_id": context.governance_project_id or request.project_id,
        "target_project_id": context.target_project_id or request.project_id,
        "target_project_root": context.target_project_root,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "role": "mf_sub",
        "allocation_owner": context.allocation_owner or context.agent_id,
        "observer_allocation_owner": context.allocation_owner or context.agent_id,
        "worker_slot_id": context.worker_slot_id or context.worker_id,
        "fence_token": context.fence_token,
        "worktree_path": context.worktree_path,
        "worktree": context.worktree_path,
        "assigned_worktree": context.worktree_path,
        "branch": context.branch_ref,
        "branch_ref": context.branch_ref,
        "head_commit": head_commit,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "route_id": request.route_id,
        "precheck_run_id": request.precheck_run_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "graph_trace_ids": list(graph_trace_ids),
        "startup_source": "observer_runtime_text_prepare",
        "startup_timing": "generated_prelaunch",
        "actual_startup_must_include": [
            "actual_host_worker_id",
            "actual_cwd",
            "actual_git_root",
            "fence_token",
            "branch",
            "head_commit",
            "route_context_hash",
            "prompt_contract_id",
            "observer_command_id",
        ],
    }
    return {
        "schema_version": "mf_subagent_startup_intent_event.v1",
        "project_id": request.project_id,
        "task_id": context.task_id,
        "backlog_id": request.backlog_id,
        "attempt_num": context.attempt,
        "event_type": "mf_subagent.startup_intent",
        "event_kind": "mf_subagent_startup_intent",
        "phase": "startup_intent",
        "actor": "observer_runtime_text",
        "status": "planned",
        "close_satisfying": False,
        "actual_startup_required": True,
        "observer_command_id": observer_command_id,
        "payload": {
            "mf_subagent_startup_intent": startup_intent,
            "observer_command_id": observer_command_id,
            "runtime_context_id": runtime_context_id,
            "graph_trace_ids": list(graph_trace_ids),
        },
        "artifact_refs": {
            "runtime_context_id": runtime_context_id,
            "observer_command_id": observer_command_id,
            "launch_text_hash": launch_text_hash,
        },
    }


def _dogfood_cli_timeout_blocker(
    request: DogfoodObserverPlanRequest,
    *,
    context: Any,
    worker_worktree: str | Path,
    owned_files: Sequence[str],
    observer_result: Mapping[str, Any],
    executable_worker_launch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    invocation = observer_result.get("invocation") if isinstance(observer_result, Mapping) else {}
    invocation = invocation if isinstance(invocation, Mapping) else {}
    diff_scope = _worker_worktree_diff_scope(
        worktree=worker_worktree,
        base_commit=context.base_commit,
        owned_files=owned_files,
    )
    no_output = bool(invocation.get("output_empty", True))
    invocation_blocker_id = str(invocation.get("blocker_id") or "")
    observer_command_id = request.task_id or context.task_id
    executable_worker_launch = (
        executable_worker_launch
        if isinstance(executable_worker_launch, Mapping)
        else observer_result.get("executable_worker_launch")
        if isinstance(observer_result.get("executable_worker_launch"), Mapping)
        else {}
    )
    runtime_context_id = request.runtime_context_id or runtime_context_id_for_branch_context(
        context
    )
    route_identity = {
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
    }
    parent_task_id = str(request.backlog_id or context.backlog_id or "")
    startup_status = _startup_read_receipt_recording_status(observer_result)
    timeline_status = _timeline_startup_read_receipt_recording_status(
        project_id=request.project_id,
        backlog_id=request.backlog_id,
        task_id=str(context.task_id or request.task_id or ""),
        runtime_context_id=runtime_context_id,
        parent_task_id=parent_task_id,
        route_identity=route_identity,
    )
    startup_status = _merge_startup_read_receipt_recording_status(
        startup_status,
        timeline_status,
    )
    read_receipt_recorded = bool(startup_status.get("read_receipt_recorded"))
    startup_recorded = bool(startup_status.get("startup_recorded"))
    durable_startup_read_receipt = bool(startup_recorded and read_receipt_recorded)
    startup_close_satisfying = bool(startup_status.get("startup_close_satisfying"))
    if invocation_blocker_id:
        blocker_id = invocation_blocker_id
    else:
        blocker_id = (
            f"{request.backend_mode}_timeout_no_output_no_finish"
            if no_output
            else f"{request.backend_mode}_timeout_no_finish"
        )
    if (
        invocation_blocker_id == "codex_cli_worker_no_progress_no_read_receipt"
        and read_receipt_recorded
    ):
        blocker_id = (
            f"{request.backend_mode}_timeout_no_output_no_finish"
            if no_output
            else f"{request.backend_mode}_timeout_no_finish"
        )
    runtime_monitor = invocation.get("runtime_monitor") or {}
    runtime_monitor = runtime_monitor if isinstance(runtime_monitor, Mapping) else {}
    startup_evidence_ref = (
        (
            "mf_subagent_startup_close_satisfying"
            if startup_close_satisfying
            else "mf_subagent_startup_recorded_not_close_satisfying"
        )
        if startup_recorded
        else "mf_subagent_startup_not_recorded"
    )
    read_receipt_evidence_ref = (
        "mf_subagent_read_receipt_recorded"
        if read_receipt_recorded
        else "mf_subagent_read_receipt_not_recorded"
    )
    terminal_projection = {
        "schema_version": "observer_command_terminal_projection.v1",
        "passed": False,
        "canonical_contract_state": "blocked",
        "command_projection_status": "failed",
        "divergence_reason": blocker_id,
        "terminal_evidence_refs": [
            "executable_worker_launch_payload",
            startup_evidence_ref,
            read_receipt_evidence_ref,
            "cli_timeout_blocker",
            "worktree_diff_scope",
        ],
        "canonical_route_identity": route_identity,
        "observer_command_id": observer_command_id,
        "executable_worker_launch_status": str(
            executable_worker_launch.get("status") or ""
        ),
        "missing_launch_fields": list(
            executable_worker_launch.get("missing_fields") or []
        ),
    }
    executable_payload = (
        executable_worker_launch.get("payload")
        if isinstance(executable_worker_launch.get("payload"), Mapping)
        else {}
    )
    registered_spawn = (
        executable_payload.get("registered_host_adapter_spawn")
        if isinstance(executable_payload.get("registered_host_adapter_spawn"), Mapping)
        else {}
    )
    worker_id = str(
        request.worker_id
        or getattr(context, "worker_id", "")
        or getattr(context, "worker_slot_id", "")
        or ""
    )
    worker_agent_id = str(
        registered_spawn.get("actual_host_worker_id")
        or registered_spawn.get("worker_agent_id")
        or registered_spawn.get("agent_id")
        or executable_payload.get("actual_host_worker_id")
        or executable_payload.get("worker_agent_id")
        or executable_payload.get("agent_id")
        or worker_id
    )
    missing_launch_fields = list(terminal_projection["missing_launch_fields"])
    next_legal_action = _bounded_worker_no_progress_next_legal_action(
        runtime_context_id=runtime_context_id,
        task_id=str(context.task_id or request.task_id or ""),
        parent_task_id=parent_task_id,
        observer_command_id=observer_command_id,
        worker_id=worker_id,
        worker_agent_id=worker_agent_id,
        merge_queue_id=str(context.merge_queue_id or request.merge_queue_id or ""),
        route_identity=route_identity,
        missing_launch_fields=missing_launch_fields,
        reason=blocker_id,
    )
    terminal_projection["next_legal_action"] = next_legal_action
    blocker = {
        "schema_version": (
            "observer_cli_no_progress_blocker.v1"
            if (
                invocation_blocker_id == "codex_cli_worker_no_progress_no_read_receipt"
                and not read_receipt_recorded
            )
            else "observer_cli_timeout_blocker.v1"
        ),
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "blocker_id": blocker_id,
        "terminal_blocker": True,
        "terminal_dispatch_blocker": True,
        "status": "blocked",
        "observer_command_id": observer_command_id,
        "task_id": context.task_id,
        "worker_id": worker_id,
        "worker_agent_id": worker_agent_id,
        "actual_host_worker_id": worker_agent_id,
        "attempt_num": context.attempt,
        **route_identity,
        "route_identity": route_identity,
        "branch": context.branch_ref,
        "worktree": str(Path(str(worker_worktree)).expanduser().resolve()),
        "fence_token": context.fence_token,
        "runtime_context_id": runtime_context_id,
        "runtime_context": asdict(context),
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "owned_files": list(owned_files),
        "backend_mode": request.backend_mode,
        "timeout_sec": request.timeout_sec,
        "early_progress_timeout_sec": request.early_progress_timeout_sec,
        "no_output": no_output,
        "no_finish_evidence": True,
        "startup_recorded": startup_recorded,
        "read_receipt_recorded": read_receipt_recorded,
        "read_receipt_recorded_before_implementation_wait": bool(
            startup_status.get("read_receipt_recorded_before_implementation_wait")
        ),
        "startup_timeline_event_id": startup_status.get("startup_timeline_event_id")
        or "",
        "read_receipt_timeline_event_id": startup_status.get(
            "read_receipt_timeline_event_id"
        )
        or "",
        "startup_read_receipt_recording_status": startup_status,
        "executable_worker_launch": dict(executable_worker_launch),
        "missing_launch_fields": list(
            executable_worker_launch.get("missing_fields") or []
        ),
        "implementation_evidence_recorded": False,
        "close_ready": False,
        "command_projection_status": "failed",
        "calls_models": False,
        "auth_status": invocation.get("auth_status", "cli_timeout"),
        "invocation_blocker_id": invocation_blocker_id,
        "runtime_monitor": runtime_monitor,
        "runtime_monitor_summary": _runtime_monitor_summary(runtime_monitor),
        "worktree_diff_scope": diff_scope,
        "terminal_contract_projection": terminal_projection,
        "next_legal_action": next_legal_action,
        "next_legal_actions": list(next_legal_action["actions"]),
        "reason": (
            "CLI backend reached a terminal timeout/no-finish condition after "
            "durable startup/read receipt evidence was recorded; the isolated "
            "worktree diff scope is reported."
            if durable_startup_read_receipt
            else (
                "CLI backend reached a terminal blocker after read receipt "
                "evidence was recorded but before startup/finish evidence; the "
                "isolated worktree diff scope is reported."
                if read_receipt_recorded
                else (
                    "CLI backend reached a terminal blocker before finish evidence; "
                    "an executable launch payload was produced, but startup/read "
                    "receipt evidence was not recorded, "
                    "and the isolated worktree diff scope is reported."
                )
            )
        ),
    }
    append_result = _append_dogfood_terminal_blocker_event(blocker)
    blocker["failure_evidence_appended"] = bool(append_result.get("ok"))
    blocker["failure_evidence_append"] = append_result
    if not append_result.get("ok"):
        blocker["failure_evidence_append_error"] = str(append_result.get("error") or "")
    return blocker


def _dogfood_launch_backend_blocker(
    request: DogfoodObserverPlanRequest,
    *,
    context: Any,
    owned_files: Sequence[str],
) -> dict[str, Any]:
    if request.backend_mode in WORKER_LAUNCH_BACKENDS:
        return {}
    observer_command_id = request.task_id or context.task_id
    return {
        "schema_version": "observer_worker_launch_backend_blocker.v1",
        "ok": False,
        "status": "blocked",
        "terminal_dispatch_blocker": True,
        "blocker_id": "missing_cli_launch_backend",
        "observer_command_id": observer_command_id,
        "task_id": context.task_id,
        "backlog_id": request.backlog_id,
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "backend_mode": request.backend_mode,
        "configured_launch_backends": sorted(WORKER_LAUNCH_BACKENDS),
        "branch": context.branch_ref,
        "worktree": context.worktree_path,
        "fence_token": context.fence_token,
        "owned_files": list(owned_files),
        "actual_startup_recorded": False,
        "read_receipt_recorded": False,
        "worktree_materialization_allowed": False,
        "reason": (
            "execute requested for a bounded mf_sub worker but no CLI/executor "
            "launch backend is configured for host worker startup"
        ),
    }


def _runtime_text_graph_first_obligations(
    *,
    project_id: str,
    task_id: str,
    parent_task_id: str,
    fence_token: str,
    governance_project_id: str = "",
    target_project_id: str = "",
    target_project_root: str = "",
) -> dict[str, Any]:
    governance_id = governance_project_id or project_id
    target_id = target_project_id or project_id
    return {
        "schema_version": "mf_subagent_graph_first_obligations.v1",
        "required": True,
        "read_receipt_required_before": [
            "graph_query",
            "startup",
            "implementation",
            "verification",
            "close_ready",
        ],
        "read_receipt_timeline_event_kind": "mf_subagent_read_receipt",
        "post_hoc_read_receipt_satisfies_gate": False,
        "query": {
            "project_id": target_id,
            "governance_project_id": governance_id,
            "target_project_id": target_id,
            "target_project_root": target_project_root,
            "query_source": "mf_subagent",
            "query_purpose": "subagent_context_build",
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": fence_token,
        },
        "minimum_before_edit": [
            "record mf_subagent_read_receipt for the visible route contract",
            "graph_query tool=query_schema",
            "graph_query tool=find_node_by_path for owned files",
        ],
        "trace_evidence_schema_version": "mf_subagent_graph_trace.v1",
        "dispatch_time_only": True,
        "counts_as_worker_graph_trace_evidence": False,
        "finish_gate_requires_worker_graph_trace": True,
        "finish_gate_query_purpose": "subagent_gate_validation",
        "finish_gate_trace_evidence_schema_version": "mf_subagent_graph_trace.v1",
    }


def _runtime_text_self_contract_lookup(
    *,
    project_id: str,
    observer_command_id: str,
    task_id: str,
    parent_task_id: str,
    fence_token: str,
    runtime_context_id: str = "",
    governance_project_id: str = "",
    target_project_id: str = "",
    target_project_root: str = "",
) -> dict[str, Any]:
    governance_id = governance_project_id or project_id
    target_id = target_project_id or project_id
    task_route = (
        f"/api/graph-governance/{governance_id}/parallel-branches/"
        f"{task_id}/runtime-contract"
    )
    context_route = (
        f"/api/graph-governance/{governance_id}/parallel-branches/"
        f"runtime-contexts/{runtime_context_id}/runtime-contract"
        if runtime_context_id
        else ""
    )
    required_query_fields = [
        "observer_command_id",
        "task_id",
        "parent_task_id",
        "worker_role",
        "fence_token",
    ]
    if runtime_context_id:
        required_query_fields.append("runtime_context_id")
    return {
        "schema_version": "mf_subagent_self_contract_lookup.v1",
        "required": True,
        "must_query_before": [
            "graph_query",
            "startup",
            "implementation",
            "verification",
            "close_ready",
        ],
        "source_of_truth": "runtime_contract_service",
        "supported_endpoints": {
            "by_task_id": task_route,
            "by_runtime_context_id": context_route,
        },
        "required_query_fields": required_query_fields,
        "query_identity": {
            "project_id": target_id,
            "observer_command_id": observer_command_id,
            "governance_project_id": governance_id,
            "target_project_id": target_id,
            "target_project_root": target_project_root,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": fence_token,
            "runtime_context_id": runtime_context_id,
        },
        "query_examples": {
            "by_task_id": {
                "method": "GET",
                "path": task_route,
                "query": {
                    "task_id": task_id,
                    "observer_command_id": observer_command_id,
                    "parent_task_id": parent_task_id,
                    "worker_role": "mf_sub",
                    "fence_token": fence_token,
                },
            },
            "by_runtime_context_id": {
                "method": "GET",
                "path": context_route,
                "query": {
                    "runtime_context_id": runtime_context_id,
                    "observer_command_id": observer_command_id,
                    "task_id": task_id,
                    "parent_task_id": parent_task_id,
                    "worker_role": "mf_sub",
                    "fence_token": fence_token,
                },
            },
        },
        "cli_examples": {
            "current_state_by_runtime_context_id": [
                "python",
                "-m",
                "agent.cli",
                "runtime-context",
                "current",
                "--project-id",
                governance_id,
                "--runtime-context-id",
                runtime_context_id,
                "--observer-command-id",
                observer_command_id,
                "--parent-task-id",
                parent_task_id,
                "--fence-token",
                fence_token,
                "--view",
                "worker_view",
                "--json-output",
            ]
            if runtime_context_id
            else [],
        },
    }


def _runtime_text_supplied_projection(
    request: ObserverRuntimeTextPrepareRequest,
) -> dict[str, Any]:
    """Return a caller-supplied Runtime Context Service projection, if present."""

    direct = request.runtime_context_projection
    if isinstance(direct, Mapping) and direct:
        return dict(direct)
    evidence = request.branch_runtime_evidence
    if isinstance(evidence, Mapping):
        for key in (
            "runtime_context_projection",
            "runtime_context_current",
            "runtime_context_worker_view",
        ):
            value = evidence.get(key)
            if isinstance(value, Mapping) and value:
                return dict(value)
        nested = evidence.get("context")
        if isinstance(nested, Mapping):
            for key in ("runtime_context_projection", "runtime_context_worker_view"):
                value = nested.get(key)
                if isinstance(value, Mapping) and value:
                    return dict(value)
    return {}


def _runtime_text_projection_containers(
    projection: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    stack: list[Mapping[str, Any]] = [projection]
    seen: set[int] = set()
    projection_keys = (
        "worker_view",
        "runtime_context_worker_view",
        RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
        "current",
        "current_state",
        "current_values",
        "runtime_context_current",
        RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION,
        "gate_inputs",
        "runtime_context_gate_inputs",
        RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION,
        "close_gate_view",
        "runtime_context_close_gate_view",
        RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION,
        "branch_identity",
        "runtime_context",
        "graph_query_identity",
        "timeline_refs",
        "read_receipt",
        "read_receipt_identity",
        "read_receipt_hash_action",
        "hash_bridge",
    )
    while stack:
        current = stack.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        containers.append(current)
        for key in projection_keys:
            value = current.get(key)
            if isinstance(value, Mapping):
                stack.append(value)
        views = current.get("views")
        if isinstance(views, Mapping):
            for value in views.values():
                if isinstance(value, Mapping):
                    stack.append(value)
    return containers


def _runtime_text_projection_value(
    projection: Mapping[str, Any],
    field_name: str,
) -> Any:
    for container in _runtime_text_projection_containers(projection):
        if field_name in container:
            return container.get(field_name)
        if field_name == "owned_files" and "target_files" in container:
            return container.get("target_files")
        if field_name == "branch_ref" and "branch" in container:
            return container.get("branch")
        if field_name == "worktree_path":
            for alias in ("worktree", "assigned_worktree", "worker_worktree"):
                if alias in container:
                    return container.get(alias)
    return None


def _runtime_text_projection_field_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _runtime_text_projection_missing_diagnostic(
    *,
    field_name: str,
    gate: str,
    producer: str,
) -> dict[str, str]:
    return {
        "schema_version": "runtime_context_missing_field_diagnostic.v1",
        "field": field_name,
        "gate": gate,
        "expected_source": RUNTIME_TEXT_PROJECTION_FIELD_SOURCES.get(field_name, ""),
        "producer": producer,
        "consumer": "observer_runtime_text_prepare",
    }


def _runtime_text_local_projection(
    *,
    runtime_context_id: str,
    observer_command_id: str,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    parent_task_id: str,
    owned_files: Sequence[str],
    graph_first_obligations: Mapping[str, Any],
) -> dict[str, Any]:
    graph_query_identity = dict(graph_first_obligations.get("query") or {})
    worker_view = {
        "schema_version": RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
        "project_id": request.project_id,
        "governance_project_id": context.governance_project_id or request.project_id,
        "target_project_id": context.target_project_id or request.project_id,
        "target_project_root": context.target_project_root,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "fence_token": context.fence_token,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "owned_files": list(owned_files),
        "target_files": list(owned_files),
        "graph_query_identity": graph_query_identity,
    }
    gate_inputs = {
        "schema_version": RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION,
        **worker_view,
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
    }
    return {
        "schema_version": RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION,
        "compatibility_projection": True,
        "integration_contract": RUNTIME_TEXT_RUNTIME_CONTEXT_PROJECTION_CONTRACT,
        "worker_view": worker_view,
        "gate_inputs": gate_inputs,
        "close_gate_view": {
            "schema_version": RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION,
            **worker_view,
        },
    }


def _runtime_text_projection_gate_diagnostics(
    projection: Mapping[str, Any],
    *,
    producer: str,
) -> dict[str, Any]:
    gates = {
        "mf_subagent.startup": RUNTIME_TEXT_STARTUP_PROJECTION_FIELDS,
        "mf_subagent.finish": RUNTIME_TEXT_FINISH_PROJECTION_FIELDS,
    }
    missing: list[dict[str, str]] = []
    requirement_rows: list[dict[str, str]] = []
    for gate, fields in gates.items():
        for field_name in fields:
            requirement_rows.append(
                _runtime_text_projection_missing_diagnostic(
                    field_name=field_name,
                    gate=gate,
                    producer=producer,
                )
            )
            if not _runtime_text_projection_field_present(
                _runtime_text_projection_value(projection, field_name)
            ):
                missing.append(
                    _runtime_text_projection_missing_diagnostic(
                        field_name=field_name,
                        gate=gate,
                        producer=producer,
                    )
                )
    missing_fields = sorted({item["field"] for item in missing})
    return {
        "schema_version": "observer_runtime_text_projection_gate_diagnostics.v1",
        "projection_contract": RUNTIME_TEXT_RUNTIME_CONTEXT_PROJECTION_CONTRACT,
        "projection_schema_versions": {
            "current": RUNTIME_CONTEXT_CURRENT_SCHEMA_VERSION,
            "gate_inputs": RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION,
            "worker_view": RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
            "close_gate_view": RUNTIME_CONTEXT_CLOSE_GATE_VIEW_SCHEMA_VERSION,
        },
        "producer": producer,
        "consumer": "observer_runtime_text_prepare",
        "gates": sorted(gates),
        "required_fields": requirement_rows,
        "missing": missing,
        "missing_fields": missing_fields,
        "passed": not missing,
        "status": "passed" if not missing else "missing_required_projection_fields",
    }


def _runtime_text_finish_gate_contract(context: Any) -> dict[str, Any]:
    return {
        "schema_version": "mf_subagent_finish_gate_contract.v1",
        "required": True,
        "local_function": (
            "agent.governance.mf_subagent_contract.validate_mf_subagent_finish_gate"
        ),
        "stop_states": ["review_ready", "waiting_merge", "blocked"],
        "forbidden_actions": [
            "merge",
            "push",
            "activate_graph",
            "release_gate",
            "create_task",
            "delete_worktree",
            "modify_merge_queue",
        ],
        "expected": {
            "task_id": context.task_id,
            "fence_token": context.fence_token,
            "worktree_path": context.worktree_path,
            "assigned_worktree": context.worktree_path,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
            "merge_queue_id": context.merge_queue_id,
        },
        "close_sensitive_precheck": {
            "parent_main_status_short_must_be_clean": True,
            "actual_cwd_must_equal_assigned_worktree": True,
            "actual_git_root_must_equal_assigned_worktree": True,
            "changed_files_must_be_within_owned_files": True,
        },
        "worker_graph_trace_evidence": {
            "required": True,
            "schema_version": "mf_subagent_graph_trace.v1",
            "query_source": "mf_subagent",
            "query_purpose": "subagent_gate_validation",
            "recorded_after": [
                "mf_subagent_read_receipt",
                "mf_subagent_startup",
                "runtime_contract_lookup",
            ],
        },
    }


def _runtime_text_first_progress_contract(context: Any) -> dict[str, Any]:
    return {
        "schema_version": "mf_subagent_first_progress_contract.v1",
        "required": True,
        "startup_is_progress": False,
        "timeout_event_kind": "no_progress_timeout",
        "observer_progress_sources": [
            "task_timeline",
            "route_action_precheck_after_startup",
            "audited_graph_query_with_task_and_fence_identity",
            "fenced_worktree_dirty_diff",
            "branch_head_advance",
            "checkpoint",
            "finish_gate",
            "explicit_blocker",
        ],
        "expected": {
            "task_id": context.task_id,
            "fence_token": context.fence_token,
            "worktree_path": context.worktree_path,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
        },
    }


def _runtime_text_worker_prompt(
    request: ObserverRuntimeTextPrepareRequest,
    *,
    owned_files: Sequence[str],
) -> str:
    if request.prompt:
        return request.prompt
    files = ", ".join(owned_files) if owned_files else "(no owned files supplied)"
    return (
        f"Implement backlog {request.backlog_id} as a bounded mf_sub worker. "
        f"Work only in the assigned worktree and only within these owned files: {files}. "
        "Run graph-first discovery before edits, run focused tests, and stop at "
        "review_ready with structured evidence. Do not create a git commit or "
        "emit a ::git-commit final directive until finish-time worker "
        "attestation and the runtime-context finish gate have both passed. Do "
        "not merge, push, activate graph refs, mutate merge queues, delete "
        "worktrees, or expose raw private route/context-pack content."
    )


def _runtime_text_launch_text(payload: Mapping[str, Any]) -> str:
    """Build the bounded worker launch text with one canonical runtime contract JSON block.

    AC4: Deduplicate embedded JSON — branch_runtime_evidence and graph_first_obligations
    appear multiple times in the raw payload (inside dispatch_gate and at top level).
    Emit one canonical 'Runtime contract JSON' block and replace in-payload duplicates
    with a short reference line pointing to that block by hash.

    The launch_text_hash is sha256 of the resulting full text — same recipe, smaller text.
    Worker-facing required instructions remain complete.
    """
    # Build a compact copy of the payload with duplicates replaced by reference lines.
    compact_payload: dict[str, Any] = {}
    for key, value in payload.items():
        compact_payload[key] = value

    # Identify duplicated sub-objects: graph_first_obligations appears at top level AND
    # inside dispatch_gate (as dispatch_graph_obligation and graph_first_obligations).
    # branch_runtime_evidence appears at top level AND inside dispatch_gate.
    gfo = compact_payload.get("graph_first_obligations")
    bre_top = compact_payload.get("branch_runtime_evidence")
    dispatch_gate = compact_payload.get("dispatch_gate")
    if isinstance(dispatch_gate, Mapping) and (gfo is not None or bre_top is not None):
        deduped_gate = dict(dispatch_gate)
        if gfo is not None:
            gfo_json = json.dumps(gfo, sort_keys=True, separators=(",", ":"))
            gfo_hash = "sha256:" + hashlib.sha256(gfo_json.encode()).hexdigest()[:12]
            deduped_gate["dispatch_graph_obligation"] = (
                f"see Runtime contract JSON above under graph_first_obligations; hash {gfo_hash}"
            )
            deduped_gate["graph_first_obligations"] = (
                f"see Runtime contract JSON above under graph_first_obligations; hash {gfo_hash}"
            )
        if bre_top is not None:
            bre_json = json.dumps(bre_top, sort_keys=True, separators=(",", ":"))
            bre_hash = "sha256:" + hashlib.sha256(bre_json.encode()).hexdigest()[:12]
            deduped_gate["branch_runtime_evidence"] = (
                f"see Runtime contract JSON above under branch_runtime_evidence; hash {bre_hash}"
            )
        compact_payload["dispatch_gate"] = deduped_gate
    runtime_context = (
        payload.get("runtime_context")
        if isinstance(payload.get("runtime_context"), Mapping)
        else {}
    )
    local_bridge = _runtime_text_local_bridge_path(
        worktree_path=str(runtime_context.get("worktree_path") or ""),
        runtime_context_id=str(payload.get("runtime_context_id") or ""),
    )
    local_bridge_path = str(local_bridge.get("path") or "")
    test_environment_preflight = (
        payload.get("test_environment_preflight")
        if isinstance(payload.get("test_environment_preflight"), Mapping)
        else {}
    )
    test_environment_instruction = ""
    if (
        test_environment_preflight.get("setup_commands")
        or test_environment_preflight.get("test_commands")
    ):
        test_environment_instruction = (
            "Before run_focused_tests, execute "
            "`test_environment_preflight.setup_commands` from the assigned "
            "worktree when present; this creates `.venv` and installs missing "
            "pytest runner dependencies before running "
            "`test_environment_preflight.test_commands`. Record any setup "
            "failure as a worker-owned blocker.\n\n"
        )

    return (
        "You are a bounded mf_sub implementation worker for Aming Claw.\n\n"
        "Follow the runtime context exactly. Raw private route/context-pack "
        "content is outside this worker boundary; use only the bounded contract "
        "fields below.\n\n"
        "Persistent evidence must store runtime_context_id and launch_text_hash "
        "only; raw launch text is returned to the host for launch and must not be "
        "persisted in timeline evidence.\n\n"
        "Before graph query, startup, implementation, verification, or close-ready "
        "evidence can satisfy close-sensitive gates, record an "
        "mf_subagent_read_receipt for this visible route contract using the "
        "runtime contract's observer_command_id. A post-hoc read receipt after "
        "counted evidence does not satisfy the ordering gate.\n\n"
        "The observer_command_id must be the claimed backlog-specific "
        "execute_backlog_row command for this backlog and route identity. Do not "
        "substitute a blind observer_command_next result unless it is that exact "
        "claimed command.\n\n"
        "Before graph query, startup, or implementation, query your own runtime "
        "contract from the runtime contract service. Use either "
        "`/api/graph-governance/{project_id}/parallel-branches/{task_id}/runtime-contract` "
        "or `/api/graph-governance/{project_id}/parallel-branches/runtime-contexts/"
        "{runtime_context_id}/runtime-contract` when runtime_context_id is available. "
        "Every lookup must carry task_id, parent_task_id, worker_role=mf_sub, "
        "fence_token, and runtime_context_id when available. Echo parent_task_id "
        "in mf_subagent_read_receipt and mf_subagent_startup evidence.\n\n"
        "If the HTTP runtime contract or MCP runtime_context_worker_guide is "
        "unavailable before read receipt, read the local bounded bridge file "
        f"in the assigned worktree instead: `{local_bridge_path}`. The local "
        "bridge is a read-only startup map, not timeline evidence and not a "
        "gate bypass; when the assigned worktree is a git repo it is stored "
        "under the git private directory so it does not dirty owned-file scope. "
        "If you still cannot record mf_subagent_read_receipt or "
        "mf_subagent_startup through governance, stop before implementation "
        "and report blocker governance_io_unavailable_before_read_receipt. "
        "For Codex CLI workers, prefer launching with "
        "`--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check`; "
        "`--sandbox workspace-write` may prevent localhost governance or "
        "noninteractive MCP access.\n\n"
        "Record mf_subagent_startup by submitting the `startup_recording` "
        "object from the Runtime contract JSON to parallel_branch_startup; do "
        "not omit `owned_files`, route identity, observer_command_id, or "
        "read_receipt fields from that canonical payload. After any startup "
        "attempt, inspect the response body before acting. If `ok=false`, "
        "`status=blocked`, `blocked=true`, `must_stop=true`, `blocker_id` is "
        "present, `event_kind` ends in `_refusal`, or "
        "`timeline_event_recorded.event_kind=mf_subagent_startup_refusal`, "
        "stop before graph queries or implementation and report the blocker. "
        "Do not treat an event id for `mf_subagent_startup_refusal` as startup "
        "acceptance.\n\n"
        "Completion order is strict: record implementation evidence, record "
        "finish-time worker attestation, pass the runtime-context finish gate, "
        "and only then create the worker git commit. Leave the worker diff "
        "uncommitted until finish gate passes. Do not run `git commit`, do not "
        "emit a `::git-commit` final directive, and do not ask the host to "
        "commit before finish gate passes; if this cannot be satisfied, stop "
        "and report the blocker instead of backfilling evidence.\n\n"
        "Before handing off review_ready, run the local task precheck when "
        "available, normally `python -m agent.cli mf precommit-check --json-output` "
        "from the assigned worktree. Include the precheck command, exit code, "
        "result hash or artifact path, and any model-corrected contract repair in "
        "final evidence; if the precheck is not applicable, record the concrete "
        "reason.\n\n"
        f"{test_environment_instruction}"
        "Runtime contract JSON:\n"
        + json.dumps(compact_payload, indent=2, sort_keys=True)
    )


def _runtime_text_context_pack_status(
    request: ObserverRuntimeTextPrepareRequest,
) -> tuple[list[str], str, dict[str, Any]]:
    refs = _runtime_text_items(request.context_pack_refs)
    resolution = (
        dict(request.context_pack_resolution)
        if isinstance(request.context_pack_resolution, Mapping)
        else {}
    )
    status = str(
        request.context_pack_status
        or resolution.get("status")
        or resolution.get("resolution_status")
        or ""
    ).strip()
    if not status:
        status = "not_required" if not refs and not resolution else "ready"
    return refs, status, resolution


def _runtime_text_local_bridge_path(
    *,
    worktree_path: str,
    runtime_context_id: str,
) -> dict[str, Any]:
    safe_id = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in str(runtime_context_id or "").strip()
    ).strip(".-_")
    if not safe_id:
        safe_id = "runtime-context"
    safe_id = safe_id[:96]
    filename = f"{safe_id}.worker-launch-pack.json"
    git_private_relative_path = f"aming-claw/runtime-context/{filename}"
    git_dir = _runtime_text_git_dir(worktree_path)
    if git_dir:
        relative_path = git_private_relative_path
        path = str(Path(git_dir) / git_private_relative_path)
        storage = "git_private_dir"
        worktree_status_visible = False
        dirty_scope_impact = "none"
    else:
        relative_path = f".aming-claw/runtime-context/{filename}"
        path = str(Path(worktree_path) / relative_path) if worktree_path else ""
        storage = "worktree_metadata_fallback"
        worktree_status_visible = True
        dirty_scope_impact = "would_create_untracked_file"
    return {
        "schema_version": "observer_worker_launch_pack.local_bridge.v1",
        "status": "planned",
        "path": path,
        "relative_path": relative_path,
        "git_private_relative_path": git_private_relative_path,
        "storage": storage,
        "git_dir": git_dir,
        "format": "json",
        "writer": "observer_runtime_text_prepare",
        "network_dependency": False,
        "worktree_status_visible": worktree_status_visible,
        "dirty_scope_impact": dirty_scope_impact,
        "raw_launch_text_persisted": False,
        "contains": [
            "worker_launch_pack",
            "startup_recording",
            "self_contract_lookup",
            "graph_first_obligations",
            "finish_gate_contract",
            "route_identity",
        ],
        "fallback_for": [
            "runtime_contract_http_unavailable_before_read_receipt",
            "runtime_context_worker_guide_mcp_unavailable_before_read_receipt",
        ],
        "purpose": (
            "Local bounded runtime-context bridge for CLI workers that cannot "
            "reach HTTP/MCP before startup. It is not timeline evidence and "
            "does not bypass server-side gates. Git worktrees store it under "
            "the git private directory so it does not dirty the worker scope."
        ),
    }


def _runtime_text_registered_startup_identity(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
    launch_text_hash: str,
) -> dict[str, Any]:
    return build_registered_host_adapter_spawn_identity(
        project_id=request.project_id,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        launch_text_hash=launch_text_hash,
        backend_mode=request.backend_mode,
        startup_source=request.startup_source,
        task_id=str(getattr(context, "task_id", "") or request.task_id or ""),
        worker_slot_id=str(
            getattr(context, "worker_slot_id", "")
            or request.worker_id
            or getattr(context, "worker_id", "")
            or ""
        ),
        agent_id=request.host_adapter_agent_id,
        actual_host_worker_id=request.actual_host_worker_id,
        host_startup_id=request.host_startup_id,
        host_session_id=request.host_session_id,
        session_token_surrogate=request.session_token_surrogate,
    )


def _runtime_text_same_owner_session_token_startup(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
    parent_task_id: str,
    owned_files: Sequence[str],
    read_receipt_identity: Mapping[str, Any],
    launch_text_hash: str,
) -> dict[str, Any]:
    allocation_owner = str(
        getattr(context, "allocation_owner", "")
        or getattr(context, "agent_id", "")
        or request.allocation_owner
        or ""
    ).strip()
    worker_slot_id = str(
        getattr(context, "worker_slot_id", "")
        or getattr(context, "worker_id", "")
        or request.worker_id
        or ""
    ).strip()
    worktree_path = str(getattr(context, "worktree_path", "") or "").strip()
    fence_token_env = "AMING_WORKER_FENCE_TOKEN"
    fence_token_hash = _runtime_text_secret_hash(
        str(getattr(context, "fence_token", "") or "")
    )
    return {
        "schema_version": "mf_subagent_same_owner_session_token_startup.v1",
        "startup_mode": "same_owner_session_token",
        "startup_source": "multi_agent_worker",
        "primary": True,
        "copyable_default": True,
        "append_tool": "parallel_branch_startup",
        "event_kind": "mf_subagent_startup",
        "session_token_source": "env:AMING_WORKER_SESSION_TOKEN",
        "session_token_required": True,
        "session_token_evidence_type": "server_verified_required",
        "session_token_persisted": False,
        "raw_session_token_persisted": False,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "task_id": str(getattr(context, "task_id", "") or request.task_id or ""),
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "worker_id": worker_slot_id,
        "worker_slot_id": worker_slot_id,
        "allocation_owner": allocation_owner,
        "agent_id": allocation_owner,
        "actual_host_worker_id": "<fill actual_host_worker_id>",
        "worker_session_id": "<fill worker_session_id>",
        "worker_transcript_ref": "<fill worker_transcript_ref>",
        "filer_principal": "<fill filer_principal>",
        "harness_type": "<fill harness_type>",
        "fence_token_hash": fence_token_hash,
        "fence_token_env": fence_token_env,
        "fence_token_redacted": bool(fence_token_hash),
        "raw_fence_token_persisted": False,
        "branch": str(getattr(context, "branch_ref", "") or ""),
        "branch_ref": str(getattr(context, "branch_ref", "") or ""),
        "worktree_path": worktree_path,
        "actual_cwd": worktree_path,
        "actual_git_root": worktree_path,
        "base_commit": str(getattr(context, "base_commit", "") or ""),
        "target_head_commit": str(getattr(context, "target_head_commit", "") or ""),
        "merge_queue_id": str(getattr(context, "merge_queue_id", "") or ""),
        "owned_files": list(owned_files),
        "target_files": list(owned_files),
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "launch_text_hash": launch_text_hash,
        "read_receipt_identity": dict(read_receipt_identity),
        "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
        "read_receipt_hash": str(
            read_receipt_identity.get("read_receipt_hash") or ""
        ),
        "read_receipt_event_id": str(
            read_receipt_identity.get("read_receipt_event_id") or ""
        ),
        "close_satisfying": False,
        "not_finish_gate_sufficient": True,
        "worker_must_add_before_submit": [
            "session_token",
            "actual_host_worker_id",
            "worker_session_id",
            "worker_transcript_ref",
            "filer_principal",
            "harness_type",
        ],
    }


def _runtime_text_host_adapter_surrogate_startup(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
    parent_task_id: str,
    owned_files: Sequence[str],
    read_receipt_identity: Mapping[str, Any],
    launch_text_hash: str,
    registered_host_adapter_spawn: Mapping[str, Any],
) -> dict[str, Any]:
    identity = dict(registered_host_adapter_spawn)
    worker_slot_id = str(
        getattr(context, "worker_slot_id", "")
        or getattr(context, "worker_id", "")
        or request.worker_id
        or ""
    ).strip()
    worktree_path = str(getattr(context, "worktree_path", "") or "").strip()
    fence_token_env = "AMING_WORKER_FENCE_TOKEN"
    fence_token_hash = _runtime_text_secret_hash(
        str(getattr(context, "fence_token", "") or "")
    )
    return {
        "schema_version": "mf_subagent_host_adapter_surrogate_startup.v1",
        "startup_mode": "host_adapter_surrogate",
        "startup_source": str(identity.get("startup_source") or ""),
        "primary": False,
        "secondary": True,
        "append_tool": "parallel_branch_startup",
        "event_kind": "mf_subagent_startup",
        "session_token_evidence_type": "surrogate",
        "session_token_persisted": False,
        "raw_session_token_persisted": False,
        "close_satisfying": False,
        "not_finish_gate_sufficient": True,
        "registered_host_adapter_spawn": identity,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "task_id": str(getattr(context, "task_id", "") or request.task_id or ""),
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "worker_id": worker_slot_id,
        "worker_slot_id": worker_slot_id,
        "agent_id": str(identity.get("agent_id") or ""),
        "actual_host_worker_id": str(identity.get("actual_host_worker_id") or ""),
        "host_startup_id": str(identity.get("host_startup_id") or ""),
        "host_session_id": str(identity.get("host_session_id") or ""),
        "session_token_surrogate": str(identity.get("session_token_surrogate") or ""),
        "fence_token_hash": fence_token_hash,
        "fence_token_env": fence_token_env,
        "fence_token_redacted": bool(fence_token_hash),
        "raw_fence_token_persisted": False,
        "branch": str(getattr(context, "branch_ref", "") or ""),
        "branch_ref": str(getattr(context, "branch_ref", "") or ""),
        "worktree_path": worktree_path,
        "actual_cwd": worktree_path,
        "actual_git_root": worktree_path,
        "base_commit": str(getattr(context, "base_commit", "") or ""),
        "target_head_commit": str(getattr(context, "target_head_commit", "") or ""),
        "merge_queue_id": str(getattr(context, "merge_queue_id", "") or ""),
        "owned_files": list(owned_files),
        "target_files": list(owned_files),
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "launch_text_hash": launch_text_hash,
        "read_receipt_identity": dict(read_receipt_identity),
        "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
        "read_receipt_hash": str(
            read_receipt_identity.get("read_receipt_hash") or ""
        ),
        "read_receipt_event_id": str(
            read_receipt_identity.get("read_receipt_event_id") or ""
        ),
    }


def _runtime_text_worker_launch_pack(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
    parent_task_id: str,
    owned_files: Sequence[str],
    runtime_context_projection: Mapping[str, Any],
    runtime_context_projection_diagnostics: Mapping[str, Any],
    branch_runtime_evidence: Mapping[str, Any],
    dispatch_gate_validation: Mapping[str, Any],
    graph_first_obligations: Mapping[str, Any],
    read_receipt_identity: Mapping[str, Any],
    same_owner_session_token_startup: Mapping[str, Any],
    host_adapter_surrogate_startup: Mapping[str, Any],
    registered_host_adapter_spawn: Mapping[str, Any],
    launch_text_hash: str,
    runtime_context_worker_envelope_claim: Mapping[str, Any],
) -> dict[str, Any]:
    context_pack_refs, context_pack_status, context_pack_resolution = (
        _runtime_text_context_pack_status(request)
    )
    graph_query_schema_trace_id = (
        request.graph_query_schema_trace_id
        or (request.graph_trace_ids[0] if request.graph_trace_ids else "")
    )
    worker_guide_ref = request.worker_guide_ref or (
        "/api/graph-governance/{project_id}/runtime-contexts/"
        "{runtime_context_id}/worker-guide"
    )
    worker_guide_ref = worker_guide_ref.format(
        project_id=request.project_id,
        runtime_context_id=runtime_context_id,
    )
    local_bridge = _runtime_text_local_bridge_path(
        worktree_path=str(getattr(context, "worktree_path", "") or ""),
        runtime_context_id=runtime_context_id,
    )
    runtime_context_entrypoints = [
        {
            "id": "local_worker_launch_pack",
            "priority": 1,
            "method": "file",
            "path": local_bridge["path"],
            "relative_path": local_bridge["relative_path"],
            "available_after": "observer_runtime_text_prepare",
            "use_when": (
                "Use this first when the CLI worker cannot reach localhost "
                "governance or MCP tool approval is unavailable before the "
                "read receipt."
            ),
            "not_a_substitute_for": [
                "task_timeline_append",
                "parallel_branch_startup",
                "runtime_context_implementation_evidence",
            ],
        },
        {
            "id": "http_runtime_contract",
            "priority": 2,
            "method": "GET",
            "path": (
                "/api/graph-governance/{project_id}/runtime-contexts/"
                "{runtime_context_id}/runtime-contract"
            ),
            "requires_governance_network": True,
        },
        {
            "id": "mcp_runtime_context_worker_guide",
            "priority": 3,
            "method": "MCP",
            "tool": "runtime_context_worker_guide",
            "requires_mcp_tool_approval": True,
        },
    ]
    cli_runtime_requirements = {
        "schema_version": "observer_worker_launch_pack.cli_runtime_requirements.v1",
        "governance_url_env": "AMING_GOVERNANCE_URL",
        "worker_session_token_env": "AMING_WORKER_SESSION_TOKEN",
        "worker_fence_token_env": "AMING_WORKER_FENCE_TOKEN",
        "governance_network_required_for_timeline_writes": True,
        "recommended_codex_exec_flags": [
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
        ],
        "streaming_json_required_for_startup_monitor": True,
        "early_progress_signals": [
            "stdout_jsonl",
            "stderr",
            "output_last_message_file",
            "worktree_diff",
        ],
        "workspace_write_warning": (
            "Codex CLI launched with --sandbox workspace-write may be unable to "
            "reach localhost governance or use noninteractive MCP tools. If the "
            "worker cannot record mf_subagent_read_receipt before implementation, "
            "stop and relaunch with the recommended flags instead of patching."
        ),
        "failure_blocker": "governance_io_unavailable_before_read_receipt",
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
    }
    fence_token_env = "AMING_WORKER_FENCE_TOKEN"
    fence_token_hash = _runtime_text_secret_hash(
        str(getattr(context, "fence_token", "") or "")
    )
    launch_env_additions = {
        "AMING_GOVERNANCE_URL": "http://localhost:40000",
        "AMING_WORKER_SESSION_TOKEN": "<server-issued-worker-session-token>",
        "AMING_RUNTIME_CONTEXT_ID": runtime_context_id,
        "AMING_OBSERVER_COMMAND_ID": observer_command_id,
        "AMING_WORKER_TASK_ID": str(
            getattr(context, "task_id", "") or request.task_id or ""
        ),
        fence_token_env: f"<read from env:{fence_token_env} at launch time>",
    }
    launch_env_policy = _runtime_text_executable_launch_env_policy(
        launch_env_additions
    )
    cli_runtime_requirements["env_additions"] = dict(launch_env_additions)
    cli_runtime_requirements["env_policy"] = dict(launch_env_policy)
    test_environment_preflight = _runtime_text_test_environment_preflight(
        request.test_commands
    )
    normalized_test_commands = tuple(
        str(item)
        for item in (test_environment_preflight.get("test_commands") or [])
        if str(item or "").strip()
    )
    startup_alternatives = {
        "schema_version": "observer_worker_launch_pack.startup_alternatives.v1",
        "default": "same_owner_session_token_startup",
        "primary": "same_owner_session_token_startup",
        "secondary": "host_adapter_surrogate_startup",
        "same_owner_session_token_startup": dict(same_owner_session_token_startup),
        "host_adapter_surrogate_startup": dict(host_adapter_surrogate_startup),
        "raw_session_token_persisted": False,
        "raw_launch_text_persisted": False,
    }
    startup_identity_policy = _runtime_text_startup_identity_policy(
        same_owner_session_token_startup=same_owner_session_token_startup,
        host_adapter_surrogate_startup=host_adapter_surrogate_startup,
        registered_host_adapter_spawn=registered_host_adapter_spawn,
    )
    worker_guide_status = str(request.worker_guide_status or "ready").strip()
    next_legal_action = str(
        request.worker_next_legal_action or "submit_mf_subagent_read_receipt"
    ).strip()
    startup_refusal_policy = {
        "schema_version": "observer_worker_launch_pack.startup_refusal_policy.v1",
        "fail_closed": True,
        "stop_before": [
            "graph_query",
            "implementation",
            "verification",
            "finish_gate",
        ],
        "refusal_indicators": [
            "ok=false",
            "status=blocked",
            "blocked=true",
            "must_stop=true",
            "blocker_id_present",
            "event_kind_suffix=_refusal",
            "timeline_event_recorded.event_kind=mf_subagent_startup_refusal",
        ],
        "accepted_startup_event_kind": "mf_subagent_startup",
        "refusal_event_kind": "mf_subagent_startup_refusal",
        "canonical_retry_payload": "startup_recording",
        "required_retry_fields": [
            "startup_source",
            "agent_id",
            "actual_host_worker_id",
            "worker_session_id",
            "worker_transcript_ref",
            "filer_principal",
            "harness_type",
            "session_token",
            "owned_files",
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "observer_command_id",
            "read_receipt_hash",
            "read_receipt_event_id",
        ],
        "same_owner_session_token_required_fields": [
            "startup_source",
            "agent_id",
            "actual_host_worker_id",
            "worker_session_id",
            "worker_transcript_ref",
            "filer_principal",
            "harness_type",
            "session_token",
            "owned_files",
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "observer_command_id",
            "read_receipt_hash",
            "read_receipt_event_id",
        ],
        "host_adapter_surrogate_required_fields": [
            "startup_source",
            "agent_id",
            "actual_host_worker_id",
            "host_startup_id",
            "session_token_surrogate",
            "owned_files",
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "observer_command_id",
            "read_receipt_hash",
            "read_receipt_event_id",
        ],
        "operator_instruction": (
            "A recorded refusal event id is not startup acceptance; stop and "
            "report the blocker instead of continuing implementation."
        ),
        "startup_identity_policy": dict(startup_identity_policy),
    }
    required_evidence = [
        {
            "id": "runtime_context_read_receipt",
            "required": True,
            "producer": "mf_subagent_worker",
            "expected_source": "task_timeline.mf_subagent_read_receipt",
        },
        {
            "id": "mf_subagent_startup",
            "required": True,
            "producer": "mf_subagent_worker",
            "expected_source": "task_timeline.mf_subagent_startup",
        },
        {
            "id": "worker_graph_trace",
            "required": True,
            "producer": "graph_query_trace",
            "query_source": "mf_subagent",
        },
        {
            "id": "implementation_evidence",
            "required": True,
            "producer": "runtime_context.implementation_evidence",
        },
        {
            "id": "finish_time_worker_attestation",
            "required": True,
            "producer": "worker_transcript_verify",
        },
        {
            "id": "finish_gate",
            "required": True,
            "producer": "mf_subagent_worker",
            "done_state": ["review_ready", "waiting_merge"],
        },
    ]
    precommit_finish_policy = {
        "schema_version": "observer_worker_launch_pack.precommit_finish_policy.v1",
        "required": True,
        "finish_gate_before_git_commit": True,
        "worker_final_must_not_commit_before_finish_gate": True,
        "sequence": [
            "record_implementation_evidence",
            "record_finish_time_worker_attestation",
            "record_finish_gate",
            "git_commit",
            "report_review_ready_or_waiting_merge",
        ],
        "forbidden_before_finish_gate": [
            "git commit",
            "::git-commit final directive",
            "host commit request",
            "push",
            "merge",
        ],
        "allowed_after_finish_gate": [
            "git commit with Chain/Runtime trailers",
            "report_review_ready_or_waiting_merge",
        ],
        "blocker_if_unavailable": "finish_gate_before_commit_required",
    }
    worker_guide = {
        "schema_version": "worker_guide.schema.v1",
        "guide_id": f"{runtime_context_id}:worker-guide:{context.task_id}",
        "backlog_id": request.backlog_id,
        "worker_role": "mf_sub",
        "bounded_scope": {
            "runtime_context_id": runtime_context_id,
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "branch": context.branch_ref,
            "worktree_path": context.worktree_path,
            "owned_files": list(owned_files),
        },
        "worker_next_moves": [
            "read_local_worker_launch_pack_if_http_or_mcp_unavailable",
            "submit_mf_subagent_read_receipt",
            "record_mf_subagent_startup",
            "run_worker_graph_query",
            "patch_owned_files",
            "run_focused_tests",
            "record_implementation_evidence",
            "record_finish_time_worker_attestation",
            "record_finish_gate",
            "create_git_commit_after_finish_gate",
            "report_review_ready_or_waiting_merge",
        ],
        "observer_remediation": [
            "repair_route_identity",
            "resolve_context_packs",
            "refresh_runtime_context_projection",
        ],
        "constraints": {
            "allowed_actions": list(WORKER_LAUNCH_PACK_ALLOWED_ACTIONS),
            "blocked_actions": list(WORKER_LAUNCH_PACK_BLOCKED_ACTIONS),
            "raw_tokens_persisted": False,
            "worker_evidence_substitution_allowed": False,
            "precommit_finish_policy": dict(precommit_finish_policy),
        },
        "startup_alternatives": startup_alternatives,
        "startup_identity_policy": startup_identity_policy,
        "same_owner_session_token_startup": dict(same_owner_session_token_startup),
        "host_adapter_surrogate_startup": dict(host_adapter_surrogate_startup),
        "runtime_context_worker_envelope_claim": dict(
            runtime_context_worker_envelope_claim
        ),
        "startup_refusal_policy": startup_refusal_policy,
        "test_environment_preflight": test_environment_preflight,
        "tests_to_run": list(normalized_test_commands or request.test_commands),
        "evidence_to_file": required_evidence,
        "precommit_finish_policy": dict(precommit_finish_policy),
        "runtime_context_entrypoints": runtime_context_entrypoints,
        "done_state": ["review_ready", "waiting_merge"],
    }
    worker_guide_hash = request.worker_guide_hash or _stable_json_hash(worker_guide)
    blockers: list[dict[str, Any]] = []

    def add_blocker(code: str, message: str, **details: Any) -> None:
        blockers.append({"code": code, "message": message, **details})

    projection_mismatches: list[dict[str, str]] = []
    for field_name, expected in (
        ("route_id", request.route_id),
        ("route_context_hash", request.route.route_context_hash),
        ("prompt_contract_id", request.route.prompt_contract_id),
        ("prompt_contract_hash", request.route.prompt_contract_hash),
        ("route_token_ref", request.route.route_token_ref),
    ):
        actual = _runtime_text_projection_value(runtime_context_projection, field_name)
        actual_text = str(actual or "").strip()
        expected_text = str(expected or "").strip()
        if actual_text and expected_text and actual_text != expected_text:
            projection_mismatches.append(
                {
                    "field": field_name,
                    "expected": expected_text,
                    "actual": actual_text,
                }
            )
    if any(item["field"] == "route_context_hash" for item in projection_mismatches):
        add_blocker(
            "stale_route_context",
            "runtime context projection route hash differs from canonical route",
            mismatches=projection_mismatches,
        )
    if projection_mismatches:
        add_blocker(
            "stale_route_identity",
            "runtime context projection route identity differs from canonical route",
            mismatches=projection_mismatches,
        )
    if not request.route.route_token_ref:
        add_blocker(
            "missing_route_token_ref",
            "worker launch requires a route_token_ref or accepted source-event lineage",
        )
    guide_status_normalized = worker_guide_status.lower()
    if guide_status_normalized in {"", "missing", "not_found"}:
        add_blocker("missing_worker_guide", "worker guide is missing")
    elif guide_status_normalized not in {"ready", "ok", "available"}:
        add_blocker(
            "worker_guide_not_ready",
            "worker guide is present but not ready for worker launch",
            worker_guide_status=worker_guide_status,
        )
    if next_legal_action in WORKER_LAUNCH_PACK_OBSERVER_ONLY_NEXT_ACTIONS:
        add_blocker(
            "observer_only_next_action",
            "next legal action is observer-only and cannot be assigned to a worker",
            next_legal_action=next_legal_action,
        )
    if not runtime_context_projection_diagnostics.get("passed"):
        add_blocker(
            "stale_projection",
            "runtime context projection is missing required startup/finish fields",
            diagnostics=dict(runtime_context_projection_diagnostics),
        )
    if not str(getattr(context, "merge_queue_id", "") or "").strip():
        add_blocker("unresolved_merge_queue", "worker launch requires merge_queue_id")
    if not str(getattr(context, "fence_token", "") or "").strip():
        add_blocker("unresolved_fence", "worker launch requires a fence_token")
    if context_pack_status not in {
        "ready",
        "ok",
        "resolved",
        "not_required",
        "fallback_recorded",
    }:
        add_blocker(
            "unresolved_context_packs",
            "context packs are unresolved for this worker launch",
            context_pack_status=context_pack_status,
        )

    startup_prerequisites = {
        "observer_command_id": bool(observer_command_id),
        "branch_runtime_registered": not bool(
            branch_runtime_evidence.get("allocation_required")
        ),
        "route_context_hash": bool(request.route.route_context_hash),
        "prompt_contract_id": bool(request.route.prompt_contract_id),
        **{
            str(key): bool(value)
            for key, value in (
                request.startup_prerequisites.items()
                if isinstance(request.startup_prerequisites, Mapping)
                else []
            )
        },
    }
    missing_prerequisites = [
        key for key, present in startup_prerequisites.items() if not present
    ]
    if missing_prerequisites:
        add_blocker(
            "missing_startup_prerequisites",
            "worker launch is missing startup prerequisites",
            missing=missing_prerequisites,
        )

    startup_preflight = {
        "schema_version": "observer_worker_launch_pack.startup_preflight.v1",
        "allowed": not blockers,
        "status": "passed" if not blockers else "blocked",
        "fail_closed": True,
        "blockers": blockers,
        "graph_query_schema_trace_id": graph_query_schema_trace_id,
        "dispatch_gate_allowed": bool(dispatch_gate_validation.get("allowed")),
        "startup_prerequisites": startup_prerequisites,
        "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
        "read_receipt_next_action": str(
            read_receipt_identity.get("next_legal_action") or ""
        ),
    }
    pack = {
        "schema_version": OBSERVER_WORKER_LAUNCH_PACK_SCHEMA_VERSION,
        "required_fields": list(WORKER_LAUNCH_PACK_REQUIRED_FIELDS),
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "parent_task_id": parent_task_id,
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "worker_role": "mf_sub",
        "branch": context.branch_ref,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "fence_token": context.fence_token,
        "fence_token_hash": fence_token_hash,
        "fence_token_env": fence_token_env,
        "fence_token_redacted": bool(fence_token_hash),
        "raw_fence_token_persisted": False,
        "owned_files": list(owned_files),
        "merge_queue_id": context.merge_queue_id,
        "graph_query_schema_trace_id": graph_query_schema_trace_id,
        "context_pack_refs": context_pack_refs,
        "context_pack_status": context_pack_status,
        "context_pack_resolution": context_pack_resolution,
        "runtime_context_worker_envelope_claim": dict(
            runtime_context_worker_envelope_claim
        ),
        "local_runtime_context_bridge": local_bridge,
        "runtime_context_entrypoints": runtime_context_entrypoints,
        "cli_runtime_requirements": cli_runtime_requirements,
        "env_additions": dict(launch_env_additions),
        "env_policy": launch_env_policy,
        "worker_guide_ref": worker_guide_ref,
        "worker_guide_hash": worker_guide_hash,
        "worker_guide_status": worker_guide_status,
        "worker_guide": worker_guide,
        "allowed_actions": list(WORKER_LAUNCH_PACK_ALLOWED_ACTIONS),
        "blocked_actions": list(WORKER_LAUNCH_PACK_BLOCKED_ACTIONS),
        "precommit_finish_policy": dict(precommit_finish_policy),
        "next_legal_action": next_legal_action,
        "startup_preflight": startup_preflight,
        "test_environment_preflight": test_environment_preflight,
        "startup_refusal_policy": startup_refusal_policy,
        "required_evidence": required_evidence,
        "transcript_refs": _runtime_text_items(request.transcript_refs),
        "transcript_digests": _runtime_text_items(request.transcript_digests),
        "launch_text_hash": launch_text_hash,
        "read_receipt_identity": dict(read_receipt_identity),
        "startup_alternatives": startup_alternatives,
        "startup_identity_policy": startup_identity_policy,
        "same_owner_session_token_startup": dict(same_owner_session_token_startup),
        "host_adapter_surrogate_startup": dict(host_adapter_surrogate_startup),
        "startup_identity": dict(same_owner_session_token_startup),
        "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
        "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
        "read_receipt_hash": str(read_receipt_identity.get("read_receipt_hash") or ""),
        "read_receipt_event_id": str(
            read_receipt_identity.get("read_receipt_event_id") or ""
        ),
        "graph_first_obligations": dict(graph_first_obligations),
        "raw_tokens_persisted": False,
    }
    pack["worker_launch_pack_hash"] = _stable_json_hash(
        {key: value for key, value in pack.items() if key != "worker_launch_pack_hash"}
    )
    return pack


def _runtime_text_worker_output_path(
    *,
    worktree_path: str,
    runtime_context_id: str,
) -> str:
    local_bridge = _runtime_text_local_bridge_path(
        worktree_path=worktree_path,
        runtime_context_id=runtime_context_id,
    )
    bridge_path = str(local_bridge.get("path") or "").strip()
    if bridge_path:
        return str(Path(bridge_path).with_suffix(".last-message.txt"))
    safe_id = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in str(runtime_context_id or "runtime-context")
    ).strip(".-_")
    if not safe_id:
        safe_id = "runtime-context"
    return str(
        Path(str(worktree_path or "."))
        / ".aming-claw"
        / "runtime-context"
        / f"{safe_id[:96]}.last-message.txt"
    )


def _runtime_text_executable_launch_env_policy(
    env_additions: Mapping[str, Any],
) -> dict[str, Any]:
    additive_keys = [
        str(key)
        for key in env_additions.keys()
        if str(key or "").strip()
    ]
    return {
        "schema_version": OBSERVER_EXECUTABLE_LAUNCH_ENV_POLICY_SCHEMA_VERSION,
        "mode": "additive",
        "env_object_semantics": "additive_env_vars_only",
        "additive_env_keys": additive_keys,
        "preserve_host_env_keys": list(EXECUTABLE_WORKER_LAUNCH_PRESERVE_HOST_ENV),
        "must_preserve_host_env": True,
        "replacement_env_allowed": False,
        "shell_safe_prefix": True,
        "operator_instruction": (
            "Launch with the host's normal environment intact. Add only the "
            "listed AMING_* variables; do not replace the environment or use "
            "env -i. PATH/HOME/SHELL/TMPDIR must remain available so ordinary "
            "worker tools such as sed, cat, git, and Python can resolve."
        ),
        "command_display_semantics": (
            "The VAR=value prefixes in command_display are shell-safe additive "
            "assignments for one process; they do not clear the host environment."
        ),
    }


def _runtime_text_startup_identity_policy(
    *,
    same_owner_session_token_startup: Mapping[str, Any],
    host_adapter_surrogate_startup: Mapping[str, Any],
    registered_host_adapter_spawn: Mapping[str, Any],
) -> dict[str, Any]:
    host_startup_id = str(
        host_adapter_surrogate_startup.get("host_startup_id")
        or registered_host_adapter_spawn.get("host_startup_id")
        or ""
    ).strip()
    session_token_surrogate = str(
        host_adapter_surrogate_startup.get("session_token_surrogate")
        or registered_host_adapter_spawn.get("session_token_surrogate")
        or ""
    ).strip()
    same_owner_required = [
        str(item)
        for item in same_owner_session_token_startup.get(
            "worker_must_add_before_submit",
            (),
        )
    ]
    host_adapter_required = [
        "startup_source",
        "agent_id",
        "actual_host_worker_id",
        "host_startup_id",
        "session_token_surrogate",
        "owned_files",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "observer_command_id",
        "read_receipt_hash",
        "read_receipt_event_id",
    ]
    host_adapter_present = [
        field
        for field, value in (
            ("host_startup_id", host_startup_id),
            ("session_token_surrogate", session_token_surrogate),
            ("registered_host_adapter_spawn", registered_host_adapter_spawn),
        )
        if value
    ]
    return {
        "schema_version": "observer_runtime_text.startup_identity_policy.v1",
        "raw_session_token_persisted": False,
        "default": "same_owner_session_token_startup",
        "canonical_retry_payload": "startup_recording",
        "accepted_alternatives": [
            {
                "id": "same_owner_session_token_startup",
                "source": "startup_recording.same_owner_session_token_startup",
                "requires_raw_session_token": True,
                "session_token_env": "AMING_WORKER_SESSION_TOKEN",
                "required_fields": same_owner_required,
            },
            {
                "id": "host_adapter_surrogate_startup",
                "source": "startup_recording.host_adapter_surrogate_startup",
                "requires_raw_session_token": False,
                "required_fields": host_adapter_required,
                "present_fields": host_adapter_present,
                "host_startup_id": host_startup_id,
                "session_token_surrogate": session_token_surrogate,
                "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
                "close_satisfying": False,
            },
        ],
        "copyable_host_adapter_identity_fields": {
            "host_startup_id": host_startup_id,
            "session_token_surrogate": session_token_surrogate,
            "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
        },
        "present_fields": host_adapter_present,
        "missing_fields": [
            field
            for field, value in (
                ("host_startup_id", host_startup_id),
                ("session_token_surrogate", session_token_surrogate),
            )
            if not value
        ],
    }


def _runtime_text_missing_launch_fields(payload: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for field_name in EXECUTABLE_WORKER_LAUNCH_REQUIRED_FIELDS:
        value = payload.get(field_name)
        if field_name == "owned_files":
            if not value:
                missing.append(field_name)
            continue
        if isinstance(value, str):
            if not value.strip():
                missing.append(field_name)
            continue
        if value is None:
            missing.append(field_name)
    return missing


def _runtime_text_worker_envelope_claim(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
    parent_task_id: str,
    route_identity: Mapping[str, Any],
    target_project_root: str,
) -> dict[str, Any]:
    """Return a copy-safe claim bridge for worker-side host envelope recovery."""

    task_id = str(getattr(context, "task_id", "") or request.task_id or "").strip()
    worker_id = str(
        getattr(context, "worker_id", "")
        or request.worker_id
        or getattr(context, "worker_slot_id", "")
        or ""
    ).strip()
    worker_slot_id = str(
        getattr(context, "worker_slot_id", "")
        or request.worker_id
        or getattr(context, "worker_id", "")
        or ""
    ).strip()
    branch_ref = str(getattr(context, "branch_ref", "") or "").strip()
    worktree_path = str(getattr(context, "worktree_path", "") or "").strip()
    base_commit = str(getattr(context, "base_commit", "") or "").strip()
    target_head_commit = str(getattr(context, "target_head_commit", "") or "").strip()
    merge_queue_id = str(getattr(context, "merge_queue_id", "") or "").strip()
    session_token_ref = runtime_context_session_token_ref(context)
    safe_route_identity = {
        str(field): str(route_identity.get(field) or "").strip()
        for field in (
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "route_token_ref",
            "visible_injection_manifest_hash",
        )
    }
    initial_join_path = (
        f"/api/graph-governance/{request.project_id}/runtime-contexts/"
        f"{runtime_context_id}/session-token/initial-join"
    )
    rejoin_path = (
        f"/api/graph-governance/{request.project_id}/runtime-contexts/"
        f"{runtime_context_id}/session-token/rejoin"
    )
    initial_join_body = {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "observer_command_id": observer_command_id,
        "worker_role": "mf_sub",
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "target_project_root": target_project_root,
        "worktree_path": worktree_path,
        "branch_ref": branch_ref,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        **safe_route_identity,
        "reason": "Codex app worker claims first runtime-context host envelope",
        "ttl_seconds": 3600,
    }
    rejoin_body = {
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "observer_command_id": observer_command_id,
        "worker_role": "mf_sub",
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "target_project_root": target_project_root,
        "worktree_path": worktree_path,
        "branch_ref": branch_ref,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "reason": "Codex app worker lost raw runtime-context auth after startup",
        "ttl_seconds": 3600,
    }
    required_fields = [
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "target_project_root",
        "worker_role",
        "worker_id",
        "worker_slot_id",
        "worktree_path",
        "branch_ref",
        "merge_queue_id",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
    ]
    missing_fields = [
        field
        for field in required_fields
        if not str(initial_join_body.get(field) or "").strip()
    ]
    return {
        "schema_version": OBSERVER_RUNTIME_TEXT_WORKER_ENVELOPE_CLAIM_SCHEMA_VERSION,
        "status": "ready" if not missing_fields else "blocked",
        "claim_mode": "runtime_context_session_token_initial_join",
        "backend_mode": BACKEND_CODEX_APP_SUBAGENT,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "target_project_root": target_project_root,
        "worktree_path": worktree_path,
        "branch": branch_ref,
        "branch_ref": branch_ref,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "route_identity": dict(safe_route_identity),
        "session_token_ref": session_token_ref,
        "worker_session_token_ref": session_token_ref,
        "session_token_ref_source": (
            "runtime_context_session_token_ref"
            if session_token_ref
            else "initial_join.response.session_token_ref"
        ),
        "session_token_ref_expected_after_initial_join": True,
        "required_fields": required_fields,
        "missing_fields": missing_fields,
        "host_env_injection_required": False,
        "worker_claims_host_envelope": True,
        "host_envelope_delivery": "runtime_context_session_token_initial_join",
        "host_envelope_expected_fields": [
            "AMING_WORKER_SESSION_TOKEN",
            "AMING_WORKER_FENCE_TOKEN",
            "session_token_ref",
            "worker_slot_id",
            "runtime_context_id",
        ],
        "session_token_ref_alone_authorizes_writes": False,
        "raw_tokens_in_prompt_allowed": False,
        "raw_host_envelope_persisted": False,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "initial_join": {
            "method": "POST",
            "path": initial_join_path,
            "tool": "runtime_context_session_token_initial_join",
            "facade": "runtime_context.session_token_initial_join",
            "body_source": "copy_safe_body",
            "body": dict(initial_join_body),
            "copy_safe_body": dict(initial_join_body),
            "required_before_worker_evidence": [
                "mf_subagent_read_receipt",
                "mf_subagent_startup",
            ],
        },
        "rejoin": {
            "method": "POST",
            "path": rejoin_path,
            "tool": "runtime_context_session_token_rejoin",
            "facade": "runtime_context.session_token_rejoin",
            "body_source": "copy_safe_body",
            "body": dict(rejoin_body),
            "copy_safe_body": dict(rejoin_body),
            "use_after": [
                "mf_subagent_read_receipt",
                "mf_subagent_startup",
            ],
        },
        "worker_instructions": [
            (
                "Before mf_subagent_read_receipt or mf_subagent_startup, call "
                "initial_join.copy_safe_body to obtain the live host envelope."
            ),
            (
                "Use the returned host_envelope env values only in the live "
                "worker session."
            ),
            (
                "Do not paste raw session or fence tokens into timeline, "
                "implementation evidence, finish evidence, or final messages."
            ),
            (
                "If initial_join says lineage already exists, stop and use "
                "rejoin.copy_safe_body only after the existing read "
                "receipt/startup lineage is verified."
            ),
        ],
        "security_boundary": {
            "copy_safe_claim_body": True,
            "raw_session_token_persisted_to_timeline": False,
            "raw_fence_token_persisted_to_timeline": False,
            "observer_authors_worker_evidence": False,
            "session_token_ref_alone_authorizes_writes": False,
        },
    }


def _runtime_text_executable_worker_launch(
    *,
    request: ObserverRuntimeTextPrepareRequest,
    context: Any,
    runtime_context_id: str,
    observer_command_id: str,
    parent_task_id: str,
    owned_files: Sequence[str],
    launch_text_hash: str,
    worker_launch_pack: Mapping[str, Any],
    startup_recording: Mapping[str, Any],
    runtime_context_worker_envelope_claim: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the concrete host command/payload for a real CLI worker launch."""

    requested_backend_mode = request.backend_mode or BACKEND_CODEX_CLI
    backend_mode = _canonical_worker_launch_backend(requested_backend_mode)
    worktree_path = str(getattr(context, "worktree_path", "") or "").strip()
    output_path = _runtime_text_worker_output_path(
        worktree_path=worktree_path,
        runtime_context_id=runtime_context_id,
    )
    command: list[str] = []
    unsupported_backend = ""
    app_subagent_handoff_required = backend_mode == BACKEND_CODEX_APP_SUBAGENT
    if backend_mode == BACKEND_CODEX_CLI:
        command = build_codex_exec_command(cwd=worktree_path, output_path=output_path)
    elif app_subagent_handoff_required:
        command = []
    else:
        unsupported_backend = requested_backend_mode or "missing_backend_mode"

    session_token_env = "AMING_WORKER_SESSION_TOKEN"
    fence_token_env = "AMING_WORKER_FENCE_TOKEN"
    fence_token_hash = _runtime_text_secret_hash(
        str(getattr(context, "fence_token", "") or "")
    )
    env_template = {
        "AMING_GOVERNANCE_URL": "http://localhost:40000",
        session_token_env: "<server-issued-worker-session-token>",
        "AMING_RUNTIME_CONTEXT_ID": runtime_context_id,
        "AMING_OBSERVER_COMMAND_ID": observer_command_id,
        "AMING_WORKER_TASK_ID": str(getattr(context, "task_id", "") or request.task_id or ""),
        fence_token_env: f"<read from env:{fence_token_env} at launch time>",
    }
    env_policy = _runtime_text_executable_launch_env_policy(env_template)
    startup_identity_policy = (
        startup_recording.get("startup_identity_policy")
        if isinstance(startup_recording.get("startup_identity_policy"), Mapping)
        else {}
    )
    registered_host_adapter_spawn = (
        startup_recording.get("registered_host_adapter_spawn")
        if isinstance(startup_recording.get("registered_host_adapter_spawn"), Mapping)
        else {}
    )
    startup_alternatives = (
        startup_recording.get("startup_alternatives")
        if isinstance(startup_recording.get("startup_alternatives"), Mapping)
        else {}
    )
    payload = {
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "observer_command_id": observer_command_id,
        "task_id": str(getattr(context, "task_id", "") or request.task_id or ""),
        "parent_task_id": parent_task_id,
        "runtime_context_id": runtime_context_id,
        "worker_role": "mf_sub",
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "route_token_ref": request.route.route_token_ref,
        "worktree_path": worktree_path,
        "branch": str(getattr(context, "branch_ref", "") or ""),
        "branch_ref": str(getattr(context, "branch_ref", "") or ""),
        "base_commit": str(getattr(context, "base_commit", "") or ""),
        "target_head_commit": str(getattr(context, "target_head_commit", "") or ""),
        "fence_token": str(getattr(context, "fence_token", "") or ""),
        "fence_token_hash": fence_token_hash,
        "fence_token_env": fence_token_env,
        "fence_token_redacted": bool(fence_token_hash),
        "raw_fence_token_persisted": False,
        "merge_queue_id": str(getattr(context, "merge_queue_id", "") or ""),
        "owned_files": list(owned_files),
        "launch_text_hash": launch_text_hash,
        "session_token_env": session_token_env,
        "output_path": output_path,
        "startup_payload_source": "startup_recording",
        "startup_identity_policy": dict(startup_identity_policy),
        "startup_alternatives": dict(startup_alternatives),
        "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
        "host_startup_id": str(startup_recording.get("host_startup_id") or ""),
        "host_session_id": str(startup_recording.get("host_session_id") or ""),
        "session_token_surrogate": str(
            startup_recording.get("session_token_surrogate") or ""
        ),
        "canonical_startup_identity_fields": dict(
            startup_recording.get("canonical_startup_identity_fields")
            if isinstance(startup_recording.get("canonical_startup_identity_fields"), Mapping)
            else {}
        ),
    }
    worker_envelope_claim = (
        dict(runtime_context_worker_envelope_claim)
        if isinstance(runtime_context_worker_envelope_claim, Mapping)
        else {}
    )
    missing_fields = _runtime_text_missing_launch_fields(payload)
    if unsupported_backend:
        missing_fields.append("backend_mode.codex_cli")
    if app_subagent_handoff_required and (
        str(worker_envelope_claim.get("status") or "") != "ready"
    ):
        missing_fields.extend(
            str(item)
            for item in (worker_envelope_claim.get("missing_fields") or [])
            if str(item or "").strip()
        )
        if "host_adapter.codex_app_subagent_worker_envelope_claim" not in missing_fields:
            missing_fields.append(
                "host_adapter.codex_app_subagent_worker_envelope_claim"
            )
    codex_app_bridge_ready = bool(
        app_subagent_handoff_required
        and not unsupported_backend
        and not missing_fields
        and str(worker_envelope_claim.get("status") or "") == "ready"
    )
    launch_ready = bool((command or codex_app_bridge_ready) and not missing_fields)
    command_display = ""
    if command:
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in env_template.items()
        )
        command_display = f"{env_prefix} {shlex.join(command)}"
    elif codex_app_bridge_ready:
        command_display = (
            "Host-managed Codex app subagent handoff via runtime-context "
            "worker envelope claim; no shell command is generated and no raw "
            "tokens are embedded in the prompt."
        )
    elif app_subagent_handoff_required:
        command_display = (
            "Host-managed Codex app subagent handoff; no shell command is "
            "generated until the runtime-context worker envelope claim is ready."
        )
    public_env_template = {
        key: (
            f"<read from env:{key} at launch time>"
            if key in {session_token_env, fence_token_env}
            else value
        )
        for key, value in env_template.items()
    }
    public_command_skeleton = ""
    if command:
        public_env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in public_env_template.items()
        )
        public_command_skeleton = f"{public_env_prefix} {shlex.join(command)}"
    transcript_path_suggestion = str(
        Path(output_path).with_suffix(".transcript.jsonl")
    )
    worker_guide_ref = str(worker_launch_pack.get("worker_guide_ref") or "")
    worker_guide_url = (
        f"{env_template['AMING_GOVERNANCE_URL']}{worker_guide_ref}"
        if worker_guide_ref.startswith("/")
        else worker_guide_ref
    )
    target_project_root = (
        str(request.target_project_root or "")
        or str(getattr(context, "target_project_root", "") or "")
        or worktree_path
    )
    read_receipt_path = (
        f"/api/graph-governance/{request.project_id}/runtime-contexts/"
        f"{runtime_context_id}/read-receipts"
    )
    startup_path = (
        f"/api/graph-governance/{request.project_id}/runtime-contexts/"
        f"{runtime_context_id}/startup"
    )
    route_identity = {
        "route_id": str(request.route_id or ""),
        "route_context_hash": str(request.route.route_context_hash or ""),
        "prompt_contract_id": str(request.route.prompt_contract_id or ""),
        "prompt_contract_hash": str(request.route.prompt_contract_hash or ""),
        "route_token_ref": str(request.route.route_token_ref or ""),
        "visible_injection_manifest_hash": str(
            request.visible_injection_manifest_hash or ""
        ),
    }
    required_route_identity_fields = list(route_identity)
    session_token_placeholder = (
        f"<read from env:{session_token_env} at submission time>"
    )
    fence_token_placeholder = (
        f"<read from env:{fence_token_env} at submission time>"
    )
    read_receipt_persisted_payload_skeleton = {
        "runtime_context_id": runtime_context_id,
        "task_id": payload["task_id"],
        "parent_task_id": parent_task_id,
        "fence_token_env": fence_token_env,
        "session_token_env": session_token_env,
        "target_project_root": target_project_root,
        "observer_command_id": observer_command_id,
        "worker_role": "mf_sub",
        "worker_id": str(
            worker_launch_pack.get("worker_id")
            or getattr(context, "worker_id", "")
            or ""
        ),
        "worker_slot_id": str(
            worker_launch_pack.get("worker_id")
            or getattr(context, "worker_slot_id", "")
            or getattr(context, "worker_id", "")
            or ""
        ),
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "launch_text_hash": launch_text_hash,
        "read_receipt_hash": "<worker-computed-read-receipt-hash>",
        "raw_session_token_persisted": False,
        "fence_token_hash": fence_token_hash,
        "fence_token_redacted": bool(fence_token_hash),
        "raw_fence_token_persisted": False,
        **route_identity,
    }
    read_receipt_body_skeleton = {
        "runtime_context_id": runtime_context_id,
        "task_id": payload["task_id"],
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "worker_id": str(
            worker_launch_pack.get("worker_id")
            or getattr(context, "worker_id", "")
            or ""
        ),
        "worker_slot_id": str(
            worker_launch_pack.get("worker_id")
            or getattr(context, "worker_slot_id", "")
            or getattr(context, "worker_id", "")
            or ""
        ),
        "fence_token": fence_token_placeholder,
        "session_token": session_token_placeholder,
        "fence_token_env": fence_token_env,
        "session_token_env": session_token_env,
        "target_project_root": target_project_root,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "launch_text_hash": launch_text_hash,
        "read_receipt_hash": "<worker-computed-read-receipt-hash>",
        **route_identity,
        "payload": dict(read_receipt_persisted_payload_skeleton),
    }
    read_receipt_field_pointers = {
        "top_level_post_json": (
            "read_receipt_facade_payload_skeleton.copy_safe_body"
        ),
        "do_not_post_alone": [
            "read_receipt_facade_payload_skeleton.payload",
            "read_receipt_facade_payload_skeleton.copy_safe_body.payload",
        ],
        "runtime_context_id": "copy_safe_body.runtime_context_id",
        "task_id": "copy_safe_body.task_id",
        "parent_task_id": "copy_safe_body.parent_task_id",
        "worker_id": "copy_safe_body.worker_id",
        "worker_slot_id": "copy_safe_body.worker_slot_id",
        "target_project_root": "copy_safe_body.target_project_root",
        "session_token": "copy_safe_body.session_token",
        "fence_token": "copy_safe_body.fence_token",
        "route_identity": {
            field: f"copy_safe_body.{field}"
            for field in required_route_identity_fields
        },
        "receipt_hash": (
            "copy_safe_body.read_receipt_hash or copy_safe_body.launch_text_hash"
        ),
    }
    read_receipt_payload_skeleton = {
        "method": "POST",
        "path": read_receipt_path,
        "facade": "runtime_context.read_receipts",
        "top_level_body_required": True,
        "body_is_top_level_post_json": True,
        "body_source": "copy_safe_body",
        "required_fields": [
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "worker_role",
            "worker_id",
            "worker_slot_id",
            "session_token",
            "fence_token",
            "target_project_root",
            "read_receipt_hash or launch_text_hash",
            *required_route_identity_fields,
        ],
        "required_route_identity_fields": required_route_identity_fields,
        "forbidden_shapes": [
            "nested_payload_only_identity",
            "payload_posted_without_top_level_identity",
            "worktree_path_as_target_project_root_for_write_facades",
        ],
        "field_pointers": read_receipt_field_pointers,
        "auth_fields": {
            "session_token": session_token_placeholder,
            "session_token_env": session_token_env,
            "fence_token": fence_token_placeholder,
            "fence_token_env": fence_token_env,
        },
        "auth_alternatives": {
            "runtime_context_worker_envelope_claim": {
                "source": (
                    "response.executable_worker_launch."
                    "runtime_context_worker_envelope_claim.initial_join"
                ),
                "host_env_injection_required": False,
                "raw_tokens_persisted": False,
            }
        },
        "body": read_receipt_body_skeleton,
        "copy_safe_body": dict(read_receipt_body_skeleton),
        "retry_payload": dict(read_receipt_body_skeleton),
        "payload": read_receipt_persisted_payload_skeleton,
    }
    startup_persisted_payload_skeleton = {
        "mf_subagent_startup_gate": {
            "runtime_context_id": runtime_context_id,
            "task_id": payload["task_id"],
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "observer_command_id": observer_command_id,
            "fence_token_env": fence_token_env,
            "session_token_env": session_token_env,
            "target_project_root": target_project_root,
            "worker_session_id": "<actual Codex worker session id>",
            "worker_transcript_path": transcript_path_suggestion,
            "harness_type": "codex",
            "actual_cwd": worktree_path,
            "actual_git_root": worktree_path,
            "fence_token_hash": fence_token_hash,
            "fence_token_redacted": bool(fence_token_hash),
            "raw_fence_token_persisted": False,
            "branch": payload["branch"],
            "base_commit": payload["base_commit"],
            "target_head_commit": payload["target_head_commit"],
            "read_receipt_hash": "<accepted-read-receipt-hash>",
            "read_receipt_event_id": "<accepted-read-receipt-event-id>",
            "raw_session_token_persisted": False,
        }
    }
    startup_body_skeleton = {
        "runtime_context_id": runtime_context_id,
        "task_id": payload["task_id"],
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "worker_id": str(
            worker_launch_pack.get("worker_id")
            or getattr(context, "worker_id", "")
            or ""
        ),
        "worker_slot_id": str(
            worker_launch_pack.get("worker_id")
            or getattr(context, "worker_slot_id", "")
            or getattr(context, "worker_id", "")
            or ""
        ),
        "agent_id": str(registered_host_adapter_spawn.get("agent_id") or ""),
        "actual_host_worker_id": str(
            registered_host_adapter_spawn.get("actual_host_worker_id") or ""
        ),
        "host_startup_id": str(
            registered_host_adapter_spawn.get("host_startup_id") or ""
        ),
        "host_session_id": str(
            registered_host_adapter_spawn.get("host_session_id") or ""
        ),
        "session_token_surrogate": str(
            registered_host_adapter_spawn.get("session_token_surrogate") or ""
        ),
        "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
        "observer_command_id": observer_command_id,
        "fence_token": fence_token_placeholder,
        "session_token": session_token_placeholder,
        "session_token_ref": str(
            worker_envelope_claim.get("session_token_ref")
            or "<copy from initial_join response.session_token_ref>"
        ),
        "fence_token_env": fence_token_env,
        "session_token_env": session_token_env,
        "target_project_root": target_project_root,
        "worker_session_id": "<actual Codex worker session id>",
        "worker_transcript_path": transcript_path_suggestion,
        "harness_type": "codex",
        "filer_principal": "<actual Codex worker principal>",
        "actual_cwd": worktree_path,
        "actual_git_root": worktree_path,
        "branch": payload["branch"],
        "head_commit": "<worker worktree HEAD after launch>",
        "base_commit": payload["base_commit"],
        "target_head_commit": payload["target_head_commit"],
        "read_receipt_hash": "<accepted-read-receipt-hash>",
        "read_receipt_event_id": "<accepted-read-receipt-event-id>",
    }
    startup_payload_skeleton = {
        "method": "POST",
        "path": startup_path,
        "facade": "runtime_context.startup",
        "legacy_tool": "parallel_branch_startup",
        "auth_fields": {
            "session_token": session_token_placeholder,
            "session_token_env": session_token_env,
            "fence_token": fence_token_placeholder,
            "fence_token_env": fence_token_env,
        },
        "auth_alternatives": {
            "runtime_context_worker_envelope_claim": {
                "source": (
                    "response.executable_worker_launch."
                    "runtime_context_worker_envelope_claim.initial_join"
                ),
                "host_env_injection_required": False,
                "raw_tokens_persisted": False,
            }
        },
        "body": startup_body_skeleton,
        "payload": startup_persisted_payload_skeleton,
    }
    service_dispatch_worker = {
        "runtime_context_id": runtime_context_id,
        "task_id": payload["task_id"],
        "parent_task_id": parent_task_id,
        "observer_command_id": observer_command_id,
        "worker_role": "mf_sub",
        "worker_id": read_receipt_body_skeleton["worker_id"],
        "worker_slot_id": read_receipt_body_skeleton["worker_slot_id"],
        "agent_id": "<actual host-created Codex subagent id>",
        "actual_host_worker_id": "<actual host-created Codex subagent id>",
        "worker_session_id": "<actual host-created Codex subagent session id>",
        "transcript_ref": "<host transcript ref, e.g. codex:<session-id>>",
        "worker_transcript_ref": "<host transcript ref, e.g. codex:<session-id>>",
        "session_token_ref": "<copy from initial_join response.session_token_ref>",
        "worktree_path": worktree_path,
        "branch_ref": payload["branch_ref"],
        "target_project_root": target_project_root,
        "merge_queue_id": payload["merge_queue_id"],
        **route_identity,
    }
    service_dispatch_timeline_payload = {
        "event_type": "observer.subagent.service_dispatch",
        "event_kind": "observer_subagent_service_dispatch",
        "phase": "dispatch",
        "status": "accepted",
        "task_id": parent_task_id,
        "backlog_id": request.backlog_id,
        "payload": {
            "schema_version": "observer_subagent_service_dispatch.v1",
            "observer_command_id": observer_command_id,
            "runtime_context_id": runtime_context_id,
            "task_id": payload["task_id"],
            "parent_task_id": parent_task_id,
            "merge_queue_id": payload["merge_queue_id"],
            "workers": [dict(service_dispatch_worker)],
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
            **route_identity,
        },
    }
    service_dispatch_payload_skeleton = {
        "schema_version": "observer_runtime_text.service_dispatch_payload_skeleton.v1",
        "required_before": ["runtime_context.startup"],
        "purpose": (
            "Bind the host-created Codex subagent id to the runtime allocated "
            "mf_sub worker slot before startup, so startup can validate through "
            "observer_subagent_service_dispatch instead of self-asserted identity."
        ),
        "mcp_tool": "task_timeline_append",
        "event_kind": "observer_subagent_service_dispatch",
        "body_source": "copy_safe_body",
        "copy_safe_body": service_dispatch_timeline_payload,
        "required_operator_fills": [
            "payload.workers[0].agent_id",
            "payload.workers[0].actual_host_worker_id",
            "payload.workers[0].worker_session_id",
            "payload.workers[0].transcript_ref",
            "payload.workers[0].session_token_ref",
        ],
        "worker_identity_source": "host_spawn_result",
        "session_token_ref_source": "initial_join.response.session_token_ref",
        "route_identity": dict(route_identity),
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
    }
    operator_must_fill = (
        [
            "response.launch_text",
            "host.actual_host_worker_id",
            "host.worker_session_id",
            "host.worker_transcript_ref",
            "service_dispatch_payload_skeleton.copy_safe_body.payload.workers[0].agent_id",
            "service_dispatch_payload_skeleton.copy_safe_body.payload.workers[0].session_token_ref",
        ]
        if codex_app_bridge_ready
        else [
            f"env.{session_token_env}",
            f"env.{fence_token_env}",
            "response.launch_text",
        ]
    )
    handoff_next_step = (
        {
            "action": "spawn_codex_app_subagent_with_runtime_context_bridge",
            "description": (
                "Spawn the Codex app subagent in the assigned worktree with "
                "response.launch_text. The worker must claim the runtime-context "
                "host envelope before read receipt/startup and must not persist "
                "raw tokens."
            ),
        }
        if codex_app_bridge_ready
        else {
            "action": "launch_worker_now",
            "description": (
                "Launch the worker now with this argv/env/stdin packet, then "
                "submit the read receipt facade payload and the startup facade payload."
            ),
        }
    )
    handoff_packet = {
        "schema_version": OBSERVER_EXECUTABLE_HANDOFF_PACKET_SCHEMA_VERSION,
        "public_safe": True,
        "raw_launch_text_persisted": False,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "cwd": worktree_path,
        "worktree_path": worktree_path,
        "transcript_path_suggestion": transcript_path_suggestion,
        "env_var_names": list(env_template.keys()),
        "env_placeholders": dict(public_env_template),
        "session_token_env": session_token_env,
        "fence_token_env": fence_token_env,
        "operator_must_fill": operator_must_fill,
        "command_skeleton": public_command_skeleton,
        "argv_skeleton": list(command),
        "stdin": {
            "source": "response.launch_text",
            "sha256": launch_text_hash,
            "raw_launch_text_persisted": False,
        },
        "runtime_context_id": runtime_context_id,
        "task_id": payload["task_id"],
        "parent_task_id": parent_task_id,
        "fence_token": fence_token_placeholder,
        "fence_token_hash": fence_token_hash,
        "fence_token_redacted": bool(fence_token_hash),
        "observer_command_id": observer_command_id,
        "worker_guide_ref": worker_guide_ref,
        "worker_guide_url": worker_guide_url,
        "runtime_context_worker_envelope_claim": dict(worker_envelope_claim),
        "worker_envelope_claim_source": (
            "response.executable_worker_launch.runtime_context_worker_envelope_claim"
        ),
        "service_dispatch_payload_skeleton": service_dispatch_payload_skeleton,
        "read_receipt_facade_payload_skeleton": read_receipt_payload_skeleton,
        "startup_facade_payload_skeleton": startup_payload_skeleton,
        "precommit_finish_policy": dict(
            worker_launch_pack.get("precommit_finish_policy")
            if isinstance(worker_launch_pack.get("precommit_finish_policy"), Mapping)
            else {}
        ),
        "next_step": handoff_next_step,
    }
    host_adapter_handoff = {
        "schema_version": "observer_runtime_text.host_adapter_handoff.v1",
        "backend_mode": BACKEND_CODEX_APP_SUBAGENT,
        "requested_backend_mode": requested_backend_mode,
        "status": (
            "ready_for_runtime_context_claim_bridge"
            if codex_app_bridge_ready
            else "blocked_until_runtime_context_worker_envelope_claim"
            if app_subagent_handoff_required
            else "not_applicable"
        ),
        "host_adapter": "codex_app_subagent",
        "host_tool": "multi_agent_v1.spawn_agent",
        "command_generated": False,
        "reason": (
            "Codex app subagent launch uses a worker-claimed runtime-context "
            "host envelope, so raw session/fence tokens are not embedded in "
            "the prompt or timeline."
            if codex_app_bridge_ready
            else (
                "Codex app subagent launch needs a copy-safe runtime-context "
                "worker envelope claim before read receipt/startup."
            )
        ),
        "required_host_capabilities": (
            [
                "spawn_subagent_in_assigned_worktree",
                "pass_copy_safe_runtime_context_claim_to_worker",
                "return_host_session_id",
                "return_worker_transcript_ref",
            ]
            if codex_app_bridge_ready
            else [
                "spawn_subagent_in_assigned_worktree",
                "provide_runtime_context_worker_envelope_claim",
                "return_host_session_id",
                "return_worker_transcript_ref",
            ]
        ),
        "next_legal_action": (
            "spawn_codex_app_subagent_with_runtime_context_bridge"
            if codex_app_bridge_ready
            else "repair_runtime_context_worker_envelope_claim_or_use_codex_cli"
        ),
        "fallback_backend": BACKEND_CODEX_CLI,
        "worker_next_legal_action": str(
            worker_launch_pack.get("next_legal_action") or ""
        ),
        "handoff_packet_source": "response.executable_worker_launch.handoff_packet",
        "runtime_context_worker_envelope_claim": dict(worker_envelope_claim),
        "runtime_context_worker_envelope_claim_source": (
            "response.executable_worker_launch.runtime_context_worker_envelope_claim"
        ),
        "service_dispatch_payload_skeleton": service_dispatch_payload_skeleton,
        "service_dispatch_payload_source": (
            "response.executable_worker_launch.handoff_packet."
            "service_dispatch_payload_skeleton.copy_safe_body"
        ),
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
    }
    return {
        "schema_version": OBSERVER_EXECUTABLE_WORKER_LAUNCH_SCHEMA_VERSION,
        "status": "ready" if launch_ready else "blocked",
        "executable": launch_ready,
        "host_adapter_launchable": codex_app_bridge_ready,
        "backend_mode": backend_mode,
        "requested_backend_mode": requested_backend_mode,
        "command": command,
        "command_display": public_command_skeleton or command_display,
        "command_display_public_safe": True,
        "secret_launch_envelope": {
            "public_safe": False,
            "command": list(command),
            "env_source": "response.executable_worker_launch.env",
            "raw_launch_text_source": "response.launch_text",
        },
        "argv_skeleton": list(command),
        "cwd": worktree_path,
        "worktree_path": worktree_path,
        "transcript_path_suggestion": transcript_path_suggestion,
        "stdin": {
            "source": "response.launch_text",
            "sha256": launch_text_hash,
            "raw_launch_text_persisted": False,
        },
        "env": env_template,
        "env_additions": dict(env_template),
        "env_policy": env_policy,
        "environment_policy": env_policy,
        "operator_must_fill": operator_must_fill,
        "payload": payload,
        "required_fields": list(EXECUTABLE_WORKER_LAUNCH_REQUIRED_FIELDS),
        "missing_fields": missing_fields,
        "startup_payload_source": "startup_recording",
        "startup_identity_policy": dict(startup_identity_policy),
        "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
        "startup_recording": dict(startup_recording),
        "runtime_context_worker_envelope_claim": dict(worker_envelope_claim),
        "service_dispatch_payload_skeleton": service_dispatch_payload_skeleton,
        "worker_launch_pack_hash": str(
            worker_launch_pack.get("worker_launch_pack_hash") or ""
        ),
        "worker_launch_pack": dict(worker_launch_pack),
        "handoff_packet": handoff_packet,
        "public_safe_executable_handoff_packet": handoff_packet,
        "host_adapter_handoff": dict(host_adapter_handoff),
        "shell_safe": bool(command),
        "raw_launch_text_persisted": False,
        "raw_session_token_persisted": False,
        "repair": {
            "copyable_command": public_command_skeleton,
            "payload_source": "response.executable_worker_launch.payload",
            "stdin_source": "response.launch_text",
            "env_policy_source": "response.executable_worker_launch.env_policy",
            "handoff_packet_source": "response.executable_worker_launch.handoff_packet",
            "host_adapter_handoff_source": "response.executable_worker_launch.host_adapter_handoff",
            "missing_fields": missing_fields,
        },
    }


def _runtime_text_observer_next_legal_action(
    *,
    ok: bool,
    executable_worker_launch: Mapping[str, Any],
    worker_launch_pack: Mapping[str, Any],
    dispatch_gate_validation: Mapping[str, Any],
) -> dict[str, Any]:
    executable_worker_launch = (
        executable_worker_launch if isinstance(executable_worker_launch, Mapping) else {}
    )
    worker_launch_pack = (
        worker_launch_pack if isinstance(worker_launch_pack, Mapping) else {}
    )
    dispatch_gate_validation = (
        dispatch_gate_validation if isinstance(dispatch_gate_validation, Mapping) else {}
    )
    payload = (
        executable_worker_launch.get("payload")
        if isinstance(executable_worker_launch.get("payload"), Mapping)
        else {}
    )
    handoff_packet = (
        executable_worker_launch.get("handoff_packet")
        if isinstance(executable_worker_launch.get("handoff_packet"), Mapping)
        else {}
    )
    worker_envelope_claim = (
        executable_worker_launch.get("runtime_context_worker_envelope_claim")
        if isinstance(
            executable_worker_launch.get("runtime_context_worker_envelope_claim"),
            Mapping,
        )
        else handoff_packet.get("runtime_context_worker_envelope_claim")
        if isinstance(handoff_packet.get("runtime_context_worker_envelope_claim"), Mapping)
        else {}
    )
    env_policy = (
        executable_worker_launch.get("env_policy")
        if isinstance(executable_worker_launch.get("env_policy"), Mapping)
        else {}
    )
    startup_identity_policy = (
        executable_worker_launch.get("startup_identity_policy")
        if isinstance(executable_worker_launch.get("startup_identity_policy"), Mapping)
        else payload.get("startup_identity_policy")
        if isinstance(payload.get("startup_identity_policy"), Mapping)
        else {}
    )
    host_adapter_handoff = (
        executable_worker_launch.get("host_adapter_handoff")
        if isinstance(executable_worker_launch.get("host_adapter_handoff"), Mapping)
        else {}
    )
    stdin = (
        executable_worker_launch.get("stdin")
        if isinstance(executable_worker_launch.get("stdin"), Mapping)
        else {}
    )
    missing_fields = [
        str(item) for item in (executable_worker_launch.get("missing_fields") or [])
    ]
    preflight = (
        worker_launch_pack.get("startup_preflight")
        if isinstance(worker_launch_pack.get("startup_preflight"), Mapping)
        else {}
    )
    blockers = preflight.get("blockers") if isinstance(preflight, Mapping) else []
    blockers = [dict(item) for item in blockers if isinstance(item, Mapping)]
    executable_ready = bool(
        executable_worker_launch.get("executable")
        and str(executable_worker_launch.get("status") or "") == "ready"
    )
    if missing_fields:
        blockers.append(
            {
                "code": "executable_worker_launch_missing_fields",
                "message": "executable worker launch is missing required fields",
                "missing_fields": missing_fields,
            }
        )
    elif not executable_ready:
        blockers.append(
            {
                "code": "executable_worker_launch_not_executable",
                "message": "executable worker launch is not ready",
                "launch_status": str(executable_worker_launch.get("status") or ""),
            }
        )
    status = "ready" if ok and executable_ready and not missing_fields else "blocked"
    next_action = {
        "schema_version": OBSERVER_RUNTIME_TEXT_NEXT_LEGAL_ACTION_SCHEMA_VERSION,
        "id": "launch_bounded_worker",
        "action": "launch_bounded_worker",
        "status": status,
        "allowed": status == "ready",
        "source": "executable_worker_launch",
        "description": (
            "Launch the worker now, then submit mf_subagent_read_receipt and "
            "mf_subagent_startup using the facade payload skeletons."
        ),
        "launch_command_source": "executable_worker_launch",
        "payload_source": "response.executable_worker_launch.payload",
        "handoff_packet_source": "response.executable_worker_launch.handoff_packet",
        "handoff_packet": dict(handoff_packet),
        "startup_payload_source": str(
            executable_worker_launch.get("startup_payload_source") or "startup_recording"
        ),
        "stdin_source": str(stdin.get("source") or "response.launch_text"),
        "command_display": str(
            handoff_packet.get("command_skeleton")
            or executable_worker_launch.get("command_display")
            or ""
        ),
        "env_policy": dict(env_policy),
        "env_policy_source": "response.executable_worker_launch.env_policy",
        "backend_mode": str(executable_worker_launch.get("backend_mode") or ""),
        "runtime_context_id": str(payload.get("runtime_context_id") or ""),
        "observer_command_id": str(payload.get("observer_command_id") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "parent_task_id": str(payload.get("parent_task_id") or ""),
        "worker_role": str(payload.get("worker_role") or "mf_sub"),
        "route_id": str(payload.get("route_id") or ""),
        "route_context_hash": str(payload.get("route_context_hash") or ""),
        "prompt_contract_id": str(payload.get("prompt_contract_id") or ""),
        "route_token_ref": str(payload.get("route_token_ref") or ""),
        "worktree_path": str(payload.get("worktree_path") or ""),
        "branch": str(payload.get("branch") or payload.get("branch_ref") or ""),
        "fence_token": str(handoff_packet.get("fence_token") or ""),
        "fence_token_env": str(handoff_packet.get("fence_token_env") or ""),
        "merge_queue_id": str(payload.get("merge_queue_id") or ""),
        "owned_files": list(payload.get("owned_files") or []),
        "payload": {
            "source": "response.executable_worker_launch.payload",
            "public_safe": False,
            "raw_fence_token_persisted_in_next_legal_action": False,
        },
        "startup_identity_policy": dict(startup_identity_policy),
        "missing_fields": missing_fields,
        "worker_next_legal_action": str(
            worker_launch_pack.get("next_legal_action") or ""
        ),
        "worker_next_legal_action_source": "worker_launch_pack.next_legal_action",
        "dispatch_gate_status": str(dispatch_gate_validation.get("status") or ""),
        "dispatch_gate_allowed": bool(dispatch_gate_validation.get("allowed")),
        "worker_launch_pack_preflight_status": str(preflight.get("status") or ""),
        "worker_launch_pack_preflight_allowed": bool(preflight.get("allowed")),
        "blockers": blockers,
        "next_step": {
            "action": "launch_worker_now",
            "description": (
                "Launch the worker now with response.executable_worker_launch, "
                "then record read receipt and startup through the runtime-context facades."
            ),
        },
        "repair": {
            "payload_source": "response.executable_worker_launch.payload",
            "command_source": "response.executable_worker_launch.handoff_packet.command_skeleton",
            "stdin_source": str(stdin.get("source") or "response.launch_text"),
            "env_policy_source": "response.executable_worker_launch.env_policy",
            "handoff_packet_source": "response.executable_worker_launch.handoff_packet",
            "missing_fields": missing_fields,
        },
    }
    dispatch_next = dispatch_gate_validation.get("next_legal_action")
    if isinstance(dispatch_next, Mapping) and status != "ready":
        next_action["blocked_by_next_legal_action"] = dict(dispatch_next)
    if (
        host_adapter_handoff
        and str(host_adapter_handoff.get("host_adapter") or "") == "codex_app_subagent"
        and str(executable_worker_launch.get("backend_mode") or "")
        == BACKEND_CODEX_APP_SUBAGENT
        and status == "ready"
    ):
        next_action.update(
            {
                "id": "spawn_codex_app_subagent_with_runtime_context_bridge",
                "action": str(
                    host_adapter_handoff.get("next_legal_action")
                    or "spawn_codex_app_subagent_with_runtime_context_bridge"
                ),
                "description": (
                    "Spawn the Codex app subagent in the assigned worktree. "
                    "The worker must claim the runtime-context host envelope "
                    "before read receipt/startup; raw tokens stay out of the "
                    "prompt and timeline."
                ),
                "host_adapter_handoff": dict(host_adapter_handoff),
                "host_adapter_handoff_source": (
                    "response.executable_worker_launch.host_adapter_handoff"
                ),
                "runtime_context_worker_envelope_claim": dict(worker_envelope_claim),
                "runtime_context_worker_envelope_claim_source": (
                    "response.executable_worker_launch."
                    "runtime_context_worker_envelope_claim"
                ),
            }
        )
    if (
        host_adapter_handoff
        and str(host_adapter_handoff.get("host_adapter") or "") == "codex_app_subagent"
        and str(executable_worker_launch.get("backend_mode") or "")
        == BACKEND_CODEX_APP_SUBAGENT
        and status != "ready"
    ):
        next_action.update(
            {
                "id": "resolve_host_adapter_handoff",
                "action": str(
                    host_adapter_handoff.get("next_legal_action")
                    or "repair_runtime_context_worker_envelope_claim_or_use_codex_cli"
                ),
                "description": (
                    "Resolve the Codex app subagent host-adapter boundary before "
                    "worker launch; do not record worker read receipt/startup "
                    "until the runtime-context worker envelope claim is ready."
                ),
                "host_adapter_handoff": dict(host_adapter_handoff),
                "host_adapter_handoff_source": (
                    "response.executable_worker_launch.host_adapter_handoff"
                ),
            }
        )
    return next_action


def build_observer_runtime_text_context(
    request: ObserverRuntimeTextPrepareRequest,
) -> dict[str, Any]:
    """Prepare host/Codex mf_sub runtime launch text without model calls."""

    main_worktree = Path(request.main_worktree or Path.cwd()).expanduser().resolve()
    workspace_root = (
        Path(request.workspace_root).expanduser().resolve()
        if request.workspace_root
        else main_worktree.parent
    )
    task_id = request.task_id or request.backlog_id
    parent_task_id = request.parent_task_id or request.backlog_id
    worker_id = request.worker_id or "runtime-text-worker"
    allocation_owner = request.allocation_owner or "observer_runtime_text"
    governance_project_id = request.governance_project_id or request.project_id
    target_project_id = request.target_project_id or request.project_id
    git_head = _git_head(main_worktree)
    base_commit = request.base_commit or git_head
    target_head_commit = request.target_head_commit or base_commit or git_head
    merge_queue_id = request.merge_queue_id or (
        "mq-runtime-text-" + _stable_suffix(request.project_id, request.backlog_id, task_id)
    )
    fence_token = request.fence_token or (
        "fence-runtime-text-"
        + _stable_suffix(
            request.project_id,
            request.backlog_id,
            task_id,
            request.route.route_context_hash,
        )
    )
    expected_branch_fields = {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "fence_token": request.fence_token,
        "base_commit": request.base_commit,
        "target_head_commit": request.target_head_commit,
        "merge_queue_id": request.merge_queue_id,
    }
    hydrated_branch_runtime_evidence = (
        _runtime_text_hydrate_persisted_branch_runtime_evidence(
            project_id=request.project_id,
            task_id=task_id,
            branch_runtime_registration_ref=request.branch_runtime_registration_ref,
            branch_runtime_evidence=request.branch_runtime_evidence,
            runtime_context_id=request.runtime_context_id,
            expected_fields=expected_branch_fields,
        )
    )
    observer_command_id = _runtime_text_observer_command_id(
        request,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
    )
    observer_command_requirement = _runtime_text_observer_command_requirement(
        observer_command_id=observer_command_id,
        backlog_id=request.backlog_id,
        route_context_hash=request.route.route_context_hash,
        prompt_contract_id=request.route.prompt_contract_id,
    )
    context = plan_branch_runtime_context(
        project_id=request.project_id,
        task_id=task_id,
        workspace_root=str(workspace_root),
        backlog_id=request.backlog_id,
        parent_task_id=parent_task_id,
        chain_id=parent_task_id,
        root_task_id=parent_task_id,
        stage_type="mf_sub_runtime_text",
        agent_id=allocation_owner,
        worker_id=worker_id,
        allocation_owner=allocation_owner,
        worker_slot_id=worker_id,
        governance_project_id=governance_project_id,
        target_project_id=target_project_id,
        target_project_root=request.target_project_root,
        attempt=request.attempt,
        branch_prefix=request.branch_prefix,
        worktree_root=request.worktree_root,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
    )
    context = _runtime_text_apply_branch_runtime_context(
        context,
        parent_task_id=parent_task_id,
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
        runtime_context_id=request.runtime_context_id,
        explicit_expected_fields=expected_branch_fields,
    )
    supplied_projection = _runtime_text_supplied_projection(request)
    owned_files = _runtime_text_owned_file_scope(
        request,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
        supplied_projection=supplied_projection,
    )
    target_files = _runtime_text_items(context.target_files) or list(owned_files)
    owned_files = (
        _runtime_text_items(context.owned_files)
        or list(owned_files)
        or list(target_files)
    )
    if target_files or owned_files:
        context = replace(
            context,
            target_files=tuple(target_files),
            owned_files=tuple(owned_files),
        )
    graph_trace_ids = _runtime_text_items(request.graph_trace_ids)
    runtime_context_id = (
        context.runtime_context_id
        or request.runtime_context_id
        or "orctx-" + _stable_suffix(
            request.project_id,
            request.backlog_id,
            task_id,
            parent_task_id,
            context.branch_ref,
            context.worktree_path,
            request.route.route_context_hash,
            request.route.prompt_contract_id,
            context.fence_token,
            context.base_commit,
            context.target_head_commit,
            length=16,
        )
    )
    parent_route_lineage = _runtime_text_parent_route_lineage(request)
    prelaunch_graph_context = _runtime_text_prelaunch_graph_context(
        graph_trace_ids=graph_trace_ids,
    )
    graph_first_obligations = _runtime_text_graph_first_obligations(
        project_id=request.project_id,
        governance_project_id=context.governance_project_id or request.project_id,
        target_project_id=context.target_project_id or request.project_id,
        target_project_root=context.target_project_root,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        fence_token=context.fence_token,
    )
    branch_runtime_evidence = _runtime_text_branch_runtime_evidence(
        project_id=request.project_id,
        context=context,
        parent_task_id=parent_task_id,
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
        runtime_context_id=request.runtime_context_id,
    )
    service_dispatch_evidence = _runtime_text_service_dispatch_evidence()
    dispatch_gate = {
        "schema_version": "mf_subagent_dispatch_gate.v1",
        "project_id": request.project_id,
        "governance_project_id": governance_project_id,
        "target_project_id": target_project_id,
        "target_project_root": request.target_project_root,
        "backlog_id": request.backlog_id,
        "observer_command_id": observer_command_id,
        "observer_command_requirement": observer_command_requirement,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "allocation_owner": context.allocation_owner or allocation_owner,
        "observer_allocation_owner": context.allocation_owner or allocation_owner,
        "worker_slot_id": context.worker_slot_id or worker_id,
        "selected_topology": request.selected_topology,
        "recommended_topology": request.recommended_topology,
        "branch": context.branch_ref,
        "worktree": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "fence_token": context.fence_token,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "owned_files": owned_files,
        "worktree_policy": {
            "worktree_role": "isolated_worker",
            "same_worktree_allowed": False,
            "target_worktree_path": str(main_worktree),
            "main_worktree_path": str(main_worktree),
        },
        "dirty_scope_check": {
            "status": "passed",
            "passed": True,
            "dirty_scope_exact_match": True,
            "dirty_files": [],
            "changed_files": [],
            "owned_files": owned_files,
            "checked_paths": owned_files,
        },
        "parent_route_lineage": parent_route_lineage,
        "dispatch_graph_obligation": graph_first_obligations,
        "graph_first_obligations": graph_first_obligations,
        "prelaunch_graph_context": prelaunch_graph_context,
        "finish_graph_trace_requirement": {
            "schema_version": "mf_subagent_finish_graph_trace_requirement.v1",
            "required": True,
            "counts_as_dispatch_evidence": False,
            "query_source": "mf_subagent",
            "query_purpose": "subagent_gate_validation",
            "task_id": context.task_id,
            "observer_command_id": observer_command_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": context.fence_token,
            "message": (
                "Dispatch graph obligation does not satisfy finish gates; the "
                "worker must record its own graph_trace_evidence after startup."
            ),
        },
        "branch_runtime_evidence": branch_runtime_evidence,
        "service_dispatch_evidence": service_dispatch_evidence,
        "route_evidence": {
            "route_id": request.route_id,
            "precheck_run_id": request.precheck_run_id,
            "route_context_hash": request.route.route_context_hash,
            "prompt_contract_id": request.route.prompt_contract_id,
            "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
            "raw_private_context_exposed": False,
        },
    }
    worker_prompt = _runtime_text_worker_prompt(request, owned_files=owned_files)
    test_environment_preflight = _runtime_text_test_environment_preflight(
        request.test_commands
    )
    normalized_test_commands = tuple(
        str(item)
        for item in (test_environment_preflight.get("test_commands") or [])
        if str(item or "").strip()
    )
    mf_subagent_input: dict[str, Any]
    input_error = ""
    try:
        mf_subagent_input = build_mf_subagent_input(
            context,
            prompt=worker_prompt,
            acceptance_criteria=request.acceptance_criteria,
            target_files=owned_files,
            test_commands=normalized_test_commands or request.test_commands,
            operator_notes=(
                "Prepared by observer runtime text service. Use bounded contract "
                "fields only; raw private route/context-pack content is not present."
            ),
            route_context_hash=request.route.route_context_hash,
            prompt_contract_id=request.route.prompt_contract_id,
            prompt_contract_hash=request.route.prompt_contract_hash,
            parent_route_lineage=parent_route_lineage,
        )
    except MfSubagentContractError as exc:
        input_error = str(exc)
        mf_subagent_input = {
            "schema_version": "mf_subagent_input.v1",
            "error": input_error,
        }

    startup_echo_contract = _runtime_text_startup_echo_contract(
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        context=context,
        parent_task_id=parent_task_id,
    )
    self_contract_lookup = _runtime_text_self_contract_lookup(
        project_id=request.project_id,
        governance_project_id=context.governance_project_id or request.project_id,
        target_project_id=context.target_project_id or request.project_id,
        target_project_root=context.target_project_root,
        observer_command_id=observer_command_id,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        fence_token=context.fence_token,
        runtime_context_id=runtime_context_id,
    )
    target_project_root = (
        str(request.target_project_root or "")
        or str(getattr(context, "target_project_root", "") or "")
        or str(getattr(context, "worktree_path", "") or "")
    )
    route_identity = {
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
    }
    runtime_context_worker_envelope_claim = _runtime_text_worker_envelope_claim(
        request=request,
        context=context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        parent_task_id=parent_task_id,
        route_identity=route_identity,
        target_project_root=target_project_root,
    )
    runtime_context_projection = (
        supplied_projection
        if supplied_projection
        else _runtime_text_local_projection(
            runtime_context_id=runtime_context_id,
            observer_command_id=observer_command_id,
            request=request,
            context=context,
            parent_task_id=parent_task_id,
            owned_files=owned_files,
            graph_first_obligations=graph_first_obligations,
        )
    )
    runtime_context_projection_diagnostics = _runtime_text_projection_gate_diagnostics(
        runtime_context_projection,
        producer=(
            "runtime_context_service"
            if supplied_projection
            else "observer_runtime_text_prepare.compatibility_projection"
        ),
    )
    finish_gate_contract = _runtime_text_finish_gate_contract(context)
    first_progress_contract = _runtime_text_first_progress_contract(context)
    runtime_context_payload = asdict(context)
    runtime_context_payload["parent_task_id"] = parent_task_id
    runtime_context_payload["target_files"] = list(context.target_files)
    runtime_context_payload["owned_files"] = list(context.owned_files)
    launch_payload = {
        "schema_version": OBSERVER_RUNTIME_TEXT_SCHEMA_VERSION,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "observer_command_requirement": observer_command_requirement,
        "runtime_context_projection": runtime_context_projection,
        "runtime_context_projection_diagnostics": runtime_context_projection_diagnostics,
        "runtime_context": runtime_context_payload,
        "branch_identity": {
            "runtime_context_id": runtime_context_id,
            "observer_command_id": observer_command_id,
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "branch_ref": context.branch_ref,
            "worktree_path": context.worktree_path,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
            "merge_queue_id": context.merge_queue_id,
            "fence_token": context.fence_token,
        },
        "dispatch_gate": dispatch_gate,
        "mf_subagent_input": mf_subagent_input,
        "prelaunch_graph_context": prelaunch_graph_context,
        "self_contract_lookup": self_contract_lookup,
        "runtime_context_worker_envelope_claim": runtime_context_worker_envelope_claim,
        "startup_echo_contract": startup_echo_contract,
        "graph_first_obligations": graph_first_obligations,
        "first_progress_contract": first_progress_contract,
        "finish_gate_contract": finish_gate_contract,
        "test_environment_preflight": test_environment_preflight,
    }
    launch_text = _runtime_text_launch_text(launch_payload)
    launch_text_hash = "sha256:" + hashlib.sha256(
        launch_text.encode("utf-8")
    ).hexdigest()
    read_receipt_identity = _runtime_text_read_receipt_identity(
        request=request,
        runtime_context_projection=runtime_context_projection,
        branch_runtime_evidence=branch_runtime_evidence,
        launch_text_hash=launch_text_hash,
    )

    try:
        dispatch_gate_validation = validate_mf_subagent_dispatch_gate(
            dispatch_gate,
            target_worktree_path=str(main_worktree),
            main_worktree_path=str(main_worktree),
        )
    except MfSubagentContractError as exc:
        dispatch_gate_validation = {"allowed": False, "error": str(exc)}

    startup_token_join_gaps: list[dict[str, Any]] = []
    if not request.route.route_token_ref:
        startup_token_join_gaps.append(
            {
                "id": "route_token_ref",
                "action": "request_server_issued_route_token_ref",
                "reason": (
                    "mf_sub startup must join a server-issued route token ref; "
                    "raw route tokens are not persisted in runtime text"
                ),
            }
        )
    if startup_token_join_gaps and not dispatch_gate_validation.get("allowed"):
        next_legal_action = {
            "id": "complete_startup_token_join",
            "action": "request_runtime_contract_with_route_token_ref",
            "missing_prerequisites": [
                str(item.get("id") or "") for item in startup_token_join_gaps
            ],
            "ordered_missing_steps": startup_token_join_gaps,
        }
        dispatch_gate_validation = {
            **dispatch_gate_validation,
            "status": dispatch_gate_validation.get("status")
            or "missing_startup_token_join",
            "missing_startup_token_join_gaps": startup_token_join_gaps,
            "missing": list(next_legal_action["missing_prerequisites"]),
            "next_legal_action": next_legal_action,
        }

    allocation_required = bool(branch_runtime_evidence.get("allocation_required"))
    if allocation_required:
        dispatch_gate_validation = {
            **dispatch_gate_validation,
            "allowed": False,
            "status": "allocation_required",
            "allocation_required": True,
            "error": (
                "branch runtime allocation is required before dispatch-ready "
                "runtime text evidence"
            ),
            "branch_runtime_evidence": branch_runtime_evidence,
        }
    projection_missing = not bool(runtime_context_projection_diagnostics.get("passed"))
    if projection_missing:
        dispatch_gate_validation = {
            **dispatch_gate_validation,
            "allowed": False,
            "status": "missing_runtime_context_projection_fields",
            "runtime_context_projection_diagnostics": runtime_context_projection_diagnostics,
            "error": "runtime context projection is missing required startup/finish fields",
        }
    observer_command_missing = (
        observer_command_requirement.get("status") != "present"
    )
    if observer_command_missing:
        dispatch_gate_validation = {
            **dispatch_gate_validation,
            "allowed": False,
            "status": "observer_command_required",
            "observer_command_requirement": observer_command_requirement,
            "error": (
                "observer runtime-text prepare requires a claimed "
                "execute_backlog_row observer_command_id before startup/read "
                "receipt evidence can be prepared"
            ),
        }
    preliminary_ok = (
        bool(dispatch_gate_validation.get("allowed"))
        and not input_error
        and not projection_missing
        and not observer_command_missing
    )
    registered_host_adapter_spawn = _runtime_text_registered_startup_identity(
        request=request,
        context=context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        launch_text_hash=launch_text_hash,
    )
    same_owner_session_token_startup = _runtime_text_same_owner_session_token_startup(
        request=request,
        context=context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        parent_task_id=parent_task_id,
        owned_files=owned_files,
        read_receipt_identity=read_receipt_identity,
        launch_text_hash=launch_text_hash,
    )
    host_adapter_surrogate_startup = _runtime_text_host_adapter_surrogate_startup(
        request=request,
        context=context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        parent_task_id=parent_task_id,
        owned_files=owned_files,
        read_receipt_identity=read_receipt_identity,
        launch_text_hash=launch_text_hash,
        registered_host_adapter_spawn=registered_host_adapter_spawn,
    )
    startup_alternatives = {
        "schema_version": "observer_runtime_text.startup_alternatives.v1",
        "default": "same_owner_session_token_startup",
        "primary": "same_owner_session_token_startup",
        "secondary": "host_adapter_surrogate_startup",
        "same_owner_session_token_startup": dict(same_owner_session_token_startup),
        "host_adapter_surrogate_startup": dict(host_adapter_surrogate_startup),
        "raw_session_token_persisted": False,
        "raw_launch_text_persisted": False,
    }
    startup_identity_policy = _runtime_text_startup_identity_policy(
        same_owner_session_token_startup=same_owner_session_token_startup,
        host_adapter_surrogate_startup=host_adapter_surrogate_startup,
        registered_host_adapter_spawn=registered_host_adapter_spawn,
    )
    worker_launch_pack = _runtime_text_worker_launch_pack(
        request=request,
        context=context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        parent_task_id=parent_task_id,
        owned_files=owned_files,
        runtime_context_projection=runtime_context_projection,
        runtime_context_projection_diagnostics=runtime_context_projection_diagnostics,
        branch_runtime_evidence=branch_runtime_evidence,
        dispatch_gate_validation=dispatch_gate_validation,
        graph_first_obligations=graph_first_obligations,
        read_receipt_identity=read_receipt_identity,
        same_owner_session_token_startup=same_owner_session_token_startup,
        host_adapter_surrogate_startup=host_adapter_surrogate_startup,
        registered_host_adapter_spawn=registered_host_adapter_spawn,
        launch_text_hash=launch_text_hash,
        runtime_context_worker_envelope_claim=runtime_context_worker_envelope_claim,
    )
    worker_launch_pack_preflight = dict(
        worker_launch_pack.get("startup_preflight") or {}
    )
    if not worker_launch_pack_preflight.get("allowed"):
        preflight_status = (
            "worker_launch_pack_preflight_failed"
            if preliminary_ok
            else str(dispatch_gate_validation.get("status") or "")
            or "worker_launch_pack_preflight_failed"
        )
        dispatch_gate_validation = {
            **dispatch_gate_validation,
            "allowed": False,
            "status": preflight_status,
            "error": "worker launch pack startup preflight failed closed",
            "worker_launch_pack_preflight": worker_launch_pack_preflight,
        }
    ok = preliminary_ok and bool(worker_launch_pack_preflight.get("allowed"))
    startup_intent_event = (
        _runtime_text_startup_intent_event(
            request=request,
            runtime_context_id=runtime_context_id,
            observer_command_id=observer_command_id,
            context=context,
            parent_task_id=parent_task_id,
            launch_text_hash=launch_text_hash,
            graph_trace_ids=graph_trace_ids,
        )
        if ok
        else {}
    )
    startup_recording = {
        **dict(same_owner_session_token_startup),
        "schema_version": "mf_subagent_startup_recording.v1",
        "required": bool(ok),
        "recorded": False,
        "close_ready": False,
        "append_tool": "parallel_branch_startup",
        "event_kind": "mf_subagent_startup",
        "observer_command_id": observer_command_id,
        "observer_command_requirement": observer_command_requirement,
        "runtime_context_id": runtime_context_id,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "worker_slot_id": context.worker_slot_id or worker_id,
        "fence_token_hash": _runtime_text_secret_hash(
            str(getattr(context, "fence_token", "") or "")
        ),
        "fence_token_env": "AMING_WORKER_FENCE_TOKEN",
        "fence_token_redacted": bool(str(getattr(context, "fence_token", "") or "")),
        "raw_fence_token_persisted": False,
        "branch": context.branch_ref,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "owned_files": list(owned_files),
        "target_files": list(owned_files),
        "launch_text_hash": launch_text_hash,
        "startup_source": str(
            same_owner_session_token_startup.get("startup_source") or ""
        ),
        "agent_id": str(same_owner_session_token_startup.get("agent_id") or ""),
        "actual_host_worker_id": str(
            same_owner_session_token_startup.get("actual_host_worker_id") or ""
        ),
        "startup_alternatives": startup_alternatives,
        "same_owner_session_token_startup": dict(same_owner_session_token_startup),
        "host_adapter_surrogate_startup": dict(host_adapter_surrogate_startup),
        "startup_identity": dict(same_owner_session_token_startup),
        "startup_identity_policy": dict(startup_identity_policy),
        "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
        "host_startup_id": str(
            registered_host_adapter_spawn.get("host_startup_id") or ""
        ),
        "host_session_id": str(
            registered_host_adapter_spawn.get("host_session_id") or ""
        ),
        "session_token_surrogate": str(
            registered_host_adapter_spawn.get("session_token_surrogate") or ""
        ),
        "canonical_startup_identity_fields": dict(
            startup_identity_policy.get("copyable_host_adapter_identity_fields") or {}
        ),
        "read_receipt_identity": dict(read_receipt_identity),
        "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
        "read_receipt_hash": str(
            read_receipt_identity.get("read_receipt_hash") or ""
        ),
        "read_receipt_event_id": str(
            read_receipt_identity.get("read_receipt_event_id") or ""
        ),
        "intent_event_kind": "mf_subagent_startup_intent",
        "intent_event_ref": "startup_intent_event" if ok else "",
        "close_satisfying": False,
        "actual_startup_required": bool(ok),
        "blocker": (
            "record actual mf_subagent_startup after worker runtime identity is known"
            if ok
            else "dispatch gate rejected before startup intent generation"
        ),
    }
    executable_worker_launch = _runtime_text_executable_worker_launch(
        request=request,
        context=context,
        runtime_context_id=runtime_context_id,
        observer_command_id=observer_command_id,
        parent_task_id=parent_task_id,
        owned_files=owned_files,
        launch_text_hash=launch_text_hash,
        worker_launch_pack=worker_launch_pack,
        startup_recording=startup_recording,
        runtime_context_worker_envelope_claim=runtime_context_worker_envelope_claim,
    )
    executable_launch_ready = bool(
        executable_worker_launch.get("executable")
        and str(executable_worker_launch.get("status") or "") == "ready"
    )
    executable_launch_blocked = bool(ok and not executable_launch_ready)
    if executable_launch_blocked:
        missing_launch_fields = [
            str(item) for item in executable_worker_launch.get("missing_fields") or []
        ]
        dispatch_gate_validation = {
            **dispatch_gate_validation,
            "allowed": False,
            "status": "executable_worker_launch_blocked",
            "error": "executable worker launch is blocked",
            "executable_worker_launch": dict(executable_worker_launch),
            "missing_launch_fields": missing_launch_fields,
        }
        ok = False
        startup_intent_event = {}
        startup_recording = {
            **startup_recording,
            "required": False,
            "actual_startup_required": False,
            "intent_event_ref": "",
            "blocker": "executable worker launch blocked before startup intent generation",
            "executable_worker_launch_blocked": True,
            "missing_launch_fields": missing_launch_fields,
        }
        executable_worker_launch = {
            **executable_worker_launch,
            "startup_recording": dict(startup_recording),
        }
    observer_next_legal_action = _runtime_text_observer_next_legal_action(
        ok=ok,
        executable_worker_launch=executable_worker_launch,
        worker_launch_pack=worker_launch_pack,
        dispatch_gate_validation=dispatch_gate_validation,
    )
    executable_handoff_packet = (
        executable_worker_launch.get("handoff_packet")
        if isinstance(executable_worker_launch.get("handoff_packet"), Mapping)
        else {}
    )
    dispatch_gate_validation = {
        **dispatch_gate_validation,
        "startup_intent_event_generated": bool(ok),
        "actual_startup_required": bool(ok),
        "actual_startup_recorded": False,
        "close_ready": False,
        "next_legal_action": observer_next_legal_action,
    }
    return {
        "ok": ok,
        "schema_version": OBSERVER_RUNTIME_TEXT_SCHEMA_VERSION,
        "service_schema_version": OBSERVER_RUNTIME_TEXT_SERVICE_SCHEMA_VERSION,
        "status": (
            "prepared"
            if ok
            else (
                "allocation_required"
                if allocation_required
                else (
                    "observer_command_required"
                    if observer_command_missing
                    else (
                        "blocked"
                        if executable_launch_blocked
                        else "rejected"
                    )
                )
            )
        ),
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "runtime_context_id": runtime_context_id,
        "observer_command_id": observer_command_id,
        "observer_command_requirement": observer_command_requirement,
        "launch_text": launch_text,
        "launch_text_hash": launch_text_hash,
        "raw_launch_text_persisted": False,
        "persistent_evidence": {
            "runtime_context_id": runtime_context_id,
            "observer_command_id": observer_command_id,
            "observer_command_requirement": observer_command_requirement,
            "launch_text_hash": launch_text_hash,
            "raw_launch_text_persisted": False,
            "dispatch_ready": ok,
            "allocation_required": allocation_required,
            "startup_intent_event_generated": bool(ok),
            "actual_startup_required": bool(ok),
            "actual_startup_recorded": False,
            "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
            "read_receipt_identity": dict(read_receipt_identity),
            "startup_alternatives": startup_alternatives,
            "startup_identity_policy": startup_identity_policy,
            "same_owner_session_token_startup": dict(same_owner_session_token_startup),
            "host_adapter_surrogate_startup": dict(host_adapter_surrogate_startup),
            "startup_identity": dict(same_owner_session_token_startup),
            "registered_host_adapter_spawn": dict(registered_host_adapter_spawn),
            "runtime_context_worker_envelope_claim": dict(
                runtime_context_worker_envelope_claim
            ),
            "close_ready": False,
            "startup_recording": startup_recording,
            "startup_intent_event": startup_intent_event,
            "executable_worker_launch": executable_worker_launch,
            "executable_handoff_packet": dict(executable_handoff_packet),
            "next_legal_action": observer_next_legal_action,
            "worker_launch_pack_hash": worker_launch_pack.get(
                "worker_launch_pack_hash",
                "",
            ),
        },
        "startup_intent_event": startup_intent_event,
        "startup_recording": startup_recording,
        "startup_alternatives": startup_alternatives,
        "startup_identity_policy": startup_identity_policy,
        "same_owner_session_token_startup": same_owner_session_token_startup,
        "host_adapter_surrogate_startup": host_adapter_surrogate_startup,
        "startup_identity": same_owner_session_token_startup,
        "registered_host_adapter_spawn": registered_host_adapter_spawn,
        "runtime_context_worker_envelope_claim": runtime_context_worker_envelope_claim,
        "executable_worker_launch": executable_worker_launch,
        "executable_handoff_packet": dict(executable_handoff_packet),
        "read_receipt_identity": read_receipt_identity,
        "read_receipt_recorded": bool(read_receipt_identity.get("recorded")),
        "read_receipt_hash": str(read_receipt_identity.get("read_receipt_hash") or ""),
        "read_receipt_event_id": str(
            read_receipt_identity.get("read_receipt_event_id") or ""
        ),
        "close_ready": False,
        "runtime_context": runtime_context_payload,
        "runtime_context_projection": runtime_context_projection,
        "runtime_context_projection_diagnostics": runtime_context_projection_diagnostics,
        "worker_launch_pack": worker_launch_pack,
        "local_runtime_context_bridge": worker_launch_pack.get(
            "local_runtime_context_bridge",
            {},
        ),
        "startup_token_join_gaps": startup_token_join_gaps,
        "next_legal_action": observer_next_legal_action,
        "branch_identity": launch_payload["branch_identity"],
        "mf_subagent_input": mf_subagent_input,
        "dispatch_gate": dispatch_gate,
        "dispatch_gate_validation": dispatch_gate_validation,
        "prelaunch_graph_context": prelaunch_graph_context,
        "branch_runtime_evidence": branch_runtime_evidence,
        "service_dispatch_evidence": service_dispatch_evidence,
        "self_contract_lookup": self_contract_lookup,
        "startup_echo_contract": startup_echo_contract,
        "graph_first_obligations": graph_first_obligations,
        "first_progress_contract": first_progress_contract,
        "finish_gate_contract": finish_gate_contract,
        "route_identity": {
            "route_id": request.route_id,
            "route_context_hash": request.route.route_context_hash,
            "prompt_contract_id": request.route.prompt_contract_id,
            "prompt_contract_hash": request.route.prompt_contract_hash,
            "route_token_ref": request.route.route_token_ref,
            "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        },
        "calls_models": False,
        "service_manager_required": False,
        "executor_worker_required": False,
        "input_error": input_error,
    }


def build_dogfood_observer_run_plan(
    request: DogfoodObserverPlanRequest,
    *,
    execute: bool = False,
    materialize_worktree: bool = False,
) -> dict[str, Any]:
    """Generate a source-backed observer dogfood plan and one-hop gate."""

    main_worktree = Path(request.main_worktree or Path.cwd()).expanduser().resolve()
    workspace_root = (
        Path(request.workspace_root).expanduser().resolve()
        if request.workspace_root
        else main_worktree.parent
    )
    task_id = request.task_id or request.backlog_id
    worker_id = request.worker_id or "dogfood-worker"
    allocation_owner = request.allocation_owner or "dogfood_observer"
    governance_project_id = request.governance_project_id or request.project_id
    target_project_id = request.target_project_id or request.project_id
    git_head = _git_head(main_worktree)
    base_commit = request.base_commit or git_head
    target_head_commit = request.target_head_commit or base_commit or git_head
    merge_queue_id = request.merge_queue_id or (
        "mq-dogfood-" + _stable_suffix(request.project_id, request.backlog_id, task_id)
    )
    fence_token = request.fence_token or (
        "fence-dogfood-"
        + _stable_suffix(
            request.project_id,
            request.backlog_id,
            task_id,
            request.route.route_context_hash,
        )
    )
    parent_task_id = request.backlog_id
    expected_branch_fields = {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "fence_token": request.fence_token,
        "base_commit": request.base_commit,
        "target_head_commit": request.target_head_commit,
        "merge_queue_id": request.merge_queue_id,
    }
    hydrated_branch_runtime_evidence = (
        _runtime_text_hydrate_persisted_branch_runtime_evidence(
            project_id=request.project_id,
            task_id=task_id,
            branch_runtime_registration_ref=request.branch_runtime_registration_ref,
            branch_runtime_evidence=request.branch_runtime_evidence,
            runtime_context_id=request.runtime_context_id,
            expected_fields=expected_branch_fields,
        )
    )
    context = plan_branch_runtime_context(
        project_id=request.project_id,
        task_id=task_id,
        workspace_root=str(workspace_root),
        backlog_id=request.backlog_id,
        stage_type="observer_dogfood",
        agent_id=allocation_owner,
        worker_id=worker_id,
        allocation_owner=allocation_owner,
        worker_slot_id=worker_id,
        governance_project_id=governance_project_id,
        target_project_id=target_project_id,
        target_project_root=request.target_project_root,
        attempt=request.attempt,
        branch_prefix=request.branch_prefix,
        worktree_root=request.worktree_root,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
    )
    context = _runtime_text_apply_branch_runtime_context(
        context,
        parent_task_id=parent_task_id,
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
        runtime_context_id=request.runtime_context_id,
        explicit_expected_fields=expected_branch_fields,
    )
    owned_files = [str(item) for item in request.owned_files if str(item or "").strip()]
    graph_trace_ids = [
        str(item) for item in request.graph_trace_ids if str(item or "").strip()
    ]
    runtime_text_request = ObserverRuntimeTextPrepareRequest(
        project_id=request.project_id,
        backlog_id=request.backlog_id,
        route=request.route,
        governance_project_id=governance_project_id,
        target_project_id=target_project_id,
        target_project_root=request.target_project_root,
        allocation_owner=context.allocation_owner or allocation_owner,
        main_worktree=str(main_worktree),
        workspace_root=str(workspace_root),
        owned_files=tuple(owned_files),
        observer_command_id=request.task_id or context.task_id,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        worker_id=worker_id,
        attempt=request.attempt,
        worktree_root=request.worktree_root,
        branch_prefix=request.branch_prefix,
        merge_queue_id=context.merge_queue_id,
        fence_token=context.fence_token,
        graph_trace_ids=tuple(graph_trace_ids),
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
        runtime_context_id=request.runtime_context_id,
        base_commit=context.base_commit,
        target_head_commit=context.target_head_commit,
        prompt=request.prompt,
        route_id=request.route_id,
        precheck_run_id=request.precheck_run_id,
        visible_injection_manifest_hash=request.visible_injection_manifest_hash,
        backend_mode=request.backend_mode,
    )
    parent_route_lineage = _runtime_text_parent_route_lineage(runtime_text_request)
    prelaunch_graph_context = _runtime_text_prelaunch_graph_context(
        graph_trace_ids=graph_trace_ids,
    )
    graph_first_obligations = _runtime_text_graph_first_obligations(
        project_id=request.project_id,
        governance_project_id=context.governance_project_id or request.project_id,
        target_project_id=context.target_project_id or request.project_id,
        target_project_root=context.target_project_root,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        fence_token=context.fence_token,
    )
    branch_runtime_evidence = _runtime_text_branch_runtime_evidence(
        project_id=request.project_id,
        context=context,
        parent_task_id=parent_task_id,
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=hydrated_branch_runtime_evidence,
        runtime_context_id=request.runtime_context_id,
    )
    service_dispatch_evidence = _runtime_text_service_dispatch_evidence()
    dispatch_gate = {
        "schema_version": "mf_subagent_dispatch_gate.v1",
        "project_id": request.project_id,
        "governance_project_id": governance_project_id,
        "target_project_id": target_project_id,
        "target_project_root": request.target_project_root,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
        "allocation_owner": context.allocation_owner or allocation_owner,
        "observer_allocation_owner": context.allocation_owner or allocation_owner,
        "worker_slot_id": context.worker_slot_id or worker_id,
        "selected_topology": RUNTIME_TEXT_DEFAULT_TOPOLOGY,
        "recommended_topology": RUNTIME_TEXT_DEFAULT_TOPOLOGY,
        "branch": context.branch_ref,
        "worktree": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "fence_token": context.fence_token,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "route_token_ref": request.route.route_token_ref,
        "owned_files": owned_files,
        "worktree_policy": {
            "worktree_role": "isolated_worker",
            "same_worktree_allowed": False,
            "target_worktree_path": str(main_worktree),
            "main_worktree_path": str(main_worktree),
        },
        "dirty_scope_check": {
            "status": "passed",
            "passed": True,
            "dirty_scope_exact_match": True,
            "dirty_files": [],
            "changed_files": [],
            "owned_files": owned_files,
            "checked_paths": owned_files,
        },
        "parent_route_lineage": parent_route_lineage,
        "dispatch_graph_obligation": graph_first_obligations,
        "graph_first_obligations": graph_first_obligations,
        "prelaunch_graph_context": prelaunch_graph_context,
        "finish_graph_trace_requirement": {
            "schema_version": "mf_subagent_finish_graph_trace_requirement.v1",
            "required": True,
            "counts_as_dispatch_evidence": False,
            "query_source": "mf_subagent",
            "query_purpose": "subagent_gate_validation",
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": context.fence_token,
            "message": (
                "Dispatch graph obligation does not satisfy finish gates; the "
                "worker must record its own graph_trace_evidence after startup."
            ),
        },
        "branch_runtime_evidence": branch_runtime_evidence,
        "service_dispatch_evidence": service_dispatch_evidence,
        "route_evidence": {
            "route_id": request.route_id,
            "precheck_run_id": request.precheck_run_id,
            "route_context_hash": request.route.route_context_hash,
            "prompt_contract_id": request.route.prompt_contract_id,
            "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
            "raw_private_context_exposed": False,
        },
    }
    result: dict[str, Any] = {
        "ok": False,
        "schema_version": DOGFOOD_OBSERVER_PLAN_SCHEMA_VERSION,
        "status": "rejected",
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "execute": execute,
        "calls_models": False,
        "auth_status": "not_invoked",
        "main_worktree": str(main_worktree),
        "runtime_context": asdict(context),
        "dispatch_gate": dispatch_gate,
        "branch_runtime_evidence": branch_runtime_evidence,
        "service_dispatch_evidence": service_dispatch_evidence,
        "prelaunch_graph_context": prelaunch_graph_context,
        "source_evidence": {
            "main_worktree": str(main_worktree),
            "workspace_root": str(workspace_root),
            "git_head": git_head,
            "base_commit_source": "cli_option" if request.base_commit else "git_head",
            "target_head_commit_source": (
                "cli_option" if request.target_head_commit else "base_commit"
            ),
        },
    }
    route_identity_validation = _dogfood_route_identity_validation(request)
    result["route_identity_validation"] = route_identity_validation
    if not route_identity_validation["allowed"]:
        result["error"] = "observer dogfood requires complete route identity evidence"
        return result
    allocation_required = bool(branch_runtime_evidence.get("allocation_required"))
    if allocation_required:
        result["status"] = "allocation_required"
        result["dispatch_gate_validation"] = {
            "allowed": False,
            "status": "allocation_required",
            "allocation_required": True,
            "error": (
                "branch runtime allocation is required before dispatch-ready "
                "runtime text evidence"
            ),
            "branch_runtime_evidence": branch_runtime_evidence,
        }
        result["error"] = "branch runtime allocation is required before dispatch-ready runtime text evidence"
        return result

    try:
        gate_validation = validate_mf_subagent_dispatch_gate(
            dispatch_gate,
            target_worktree_path=str(main_worktree),
            main_worktree_path=str(main_worktree),
        )
    except MfSubagentContractError as exc:
        result["dispatch_gate_validation"] = {
            "allowed": False,
            "error": str(exc),
        }
        return result
    result["dispatch_gate_validation"] = gate_validation
    runtime_text = build_observer_runtime_text_context(runtime_text_request)
    result["runtime_text"] = runtime_text
    executable_worker_launch = (
        runtime_text.get("executable_worker_launch")
        if isinstance(runtime_text.get("executable_worker_launch"), Mapping)
        else {}
    )
    result["executable_worker_launch"] = dict(executable_worker_launch)
    if not runtime_text.get("ok"):
        if execute:
            launch_backend_blocker = _dogfood_launch_backend_blocker(
                request,
                context=context,
                owned_files=owned_files,
            )
            if launch_backend_blocker:
                result["status"] = "blocked"
                result["launch_backend_blocker"] = launch_backend_blocker
                result["terminal_dispatch_blocker"] = True
                result["terminal_contract_projection"] = {
                    "schema_version": "observer_command_terminal_projection.v1",
                    "passed": False,
                    "canonical_contract_state": "blocked",
                    "command_projection_status": "failed",
                    "divergence_reason": launch_backend_blocker["blocker_id"],
                    "observer_command_id": launch_backend_blocker["observer_command_id"],
                }
                result["dispatch_gate_validation"] = {
                    **(runtime_text.get("dispatch_gate_validation") or gate_validation),
                    "allowed": False,
                    "status": "blocked",
                    "terminal_dispatch_blocker": True,
                    "blocker_id": launch_backend_blocker["blocker_id"],
                }
                result["worktree_materialization"] = {
                    "status": "skipped_terminal_dispatch_blocker",
                    "materialized": False,
                    "worktree": context.worktree_path,
                }
                result["error"] = launch_backend_blocker["reason"]
                return result
        result["dispatch_gate_validation"] = (
            runtime_text.get("dispatch_gate_validation") or gate_validation
        )
        result["error"] = (
            runtime_text.get("input_error")
            or (runtime_text.get("dispatch_gate_validation") or {}).get("error")
            or "observer runtime text preparation failed"
        )
        return result

    if execute:
        launch_backend_blocker = _dogfood_launch_backend_blocker(
            request,
            context=context,
            owned_files=owned_files,
        )
        if launch_backend_blocker:
            result["status"] = "blocked"
            result["launch_backend_blocker"] = launch_backend_blocker
            result["terminal_dispatch_blocker"] = True
            result["terminal_contract_projection"] = {
                "schema_version": "observer_command_terminal_projection.v1",
                "passed": False,
                "canonical_contract_state": "blocked",
                "command_projection_status": "failed",
                "divergence_reason": launch_backend_blocker["blocker_id"],
                "observer_command_id": launch_backend_blocker["observer_command_id"],
            }
            result["dispatch_gate_validation"] = {
                **result["dispatch_gate_validation"],
                "allowed": False,
                "status": "blocked",
                "terminal_dispatch_blocker": True,
                "blocker_id": launch_backend_blocker["blocker_id"],
            }
            result["worktree_materialization"] = {
                "status": "skipped_terminal_dispatch_blocker",
                "materialized": False,
                "worktree": context.worktree_path,
            }
            result["error"] = launch_backend_blocker["reason"]
            return result

    worker_worktree = Path(context.worktree_path).expanduser().resolve()
    worker_status = _git_worktree_status(worker_worktree, main_worktree=main_worktree)
    materialization = {
        "status": "existing_git_worktree"
        if worker_status["is_git_worktree"]
        else ("existing_non_git_directory" if worker_status["exists"] else "not_materialized"),
        "materialized": worker_status["is_git_worktree"],
        "worktree": str(worker_worktree),
        "worktree_status": worker_status,
    }
    if materialize_worktree:
        materialization = _materialize_worktree(
            main_worktree=main_worktree,
            context=context,
        )
    result["worktree_materialization"] = materialization
    if materialize_worktree and not materialization.get("materialized"):
        result["materialization_preflight"] = {
            "allowed": False,
            "error": "materialize_worktree requested but the gated worker worktree was not created",
            "worktree": str(worker_worktree),
        }
        return result
    if execute and not materialization.get("materialized"):
        result["execute_preflight"] = {
            "allowed": False,
            "error": "execute requires the gated worker worktree to be an isolated real git worktree",
            "worktree": str(worker_worktree),
            "worktree_status": materialization.get("worktree_status") or {},
            "executable_worker_launch": dict(executable_worker_launch),
            "missing_fields": ["worktree_path.real_git_worktree"],
        }
        return result
    observer_command_id = request.task_id or context.task_id
    launch_env = _dogfood_execute_launch_env(
        request=request,
        context=context,
        runtime_context_id=request.runtime_context_id
        or runtime_context_id_for_branch_context(context),
        observer_command_id=observer_command_id,
    )
    result["execute_launch_env"] = {
        key: value for key, value in launch_env.items() if key != "env"
    }
    if execute and not launch_env.get("allowed"):
        blocker = _dogfood_execute_env_blocker(
            request=request,
            context=context,
            launch_env=launch_env,
            executable_worker_launch=executable_worker_launch,
        )
        result.update(
            {
                "ok": False,
                "status": "blocked",
                "calls_models": False,
                "auth_status": "not_invoked",
                "execute_env_blocker": blocker,
                "terminal_dispatch_blocker": True,
                "command_projection_status": "failed",
                "canonical_contract_state": "blocked",
                "error": blocker["reason"],
            }
        )
        return result

    read_receipt_submission: dict[str, Any] = {}
    if execute:
        read_receipt_submission = _dogfood_submit_read_receipt_facade(
            request=request,
            context=context,
            launch_env=launch_env,
            runtime_text=runtime_text,
            executable_worker_launch=executable_worker_launch,
        )
        result["read_receipt_submission"] = read_receipt_submission
        if not read_receipt_submission.get("ok"):
            blocker = _dogfood_read_receipt_submission_blocker(
                request=request,
                context=context,
                submission=read_receipt_submission,
                executable_worker_launch=executable_worker_launch,
            )
            result.update(
                {
                    "ok": False,
                    "status": "blocked",
                    "calls_models": False,
                    "auth_status": "not_invoked",
                    "read_receipt_submission_blocker": blocker,
                    "terminal_dispatch_blocker": True,
                    "command_projection_status": "failed",
                    "canonical_contract_state": "blocked",
                    "terminal_contract_projection": {
                        "schema_version": "observer_command_terminal_projection.v1",
                        "passed": False,
                        "canonical_contract_state": "blocked",
                        "command_projection_status": "failed",
                        "divergence_reason": blocker["blocker_id"],
                        "observer_command_id": observer_command_id,
                    },
                    "error": blocker["reason"],
                }
            )
            return result

    observer_request = ObserverRunRequest(
        project_id=request.project_id,
        backlog_id=request.backlog_id,
        route=request.route,
        provider=request.provider,
        model=request.model,
        backend_mode=request.backend_mode,
        workspace=str(worker_worktree),
        prompt=str(runtime_text.get("launch_text") or request.prompt),
        timeout_sec=request.timeout_sec,
        early_progress_timeout_sec=request.early_progress_timeout_sec,
        dispatch_gate=dispatch_gate,
        main_worktree=str(main_worktree),
        env=launch_env.get("env") if isinstance(launch_env.get("env"), Mapping) else {},
    )

    observer_result = run_observer(observer_request, execute=execute)
    if execute:
        observer_result["executable_worker_launch"] = dict(executable_worker_launch)
        observer_result["read_receipt_submission"] = dict(read_receipt_submission)
        if read_receipt_submission.get("read_receipt_recorded"):
            read_receipt_event_id = str(
                read_receipt_submission.get("read_receipt_timeline_event_id") or ""
            )
            read_receipt_hash = str(
                read_receipt_submission.get("read_receipt_hash") or ""
            )
            observer_result["read_receipt_recorded"] = True
            observer_result["read_receipt_recorded_before_implementation_wait"] = True
            observer_result["read_receipt"] = {
                "timeline_event_id": read_receipt_event_id,
                "read_receipt_hash": read_receipt_hash,
                "hash": read_receipt_hash,
                "source": "observer_dogfood_auto_submit_read_receipt",
            }
            observer_result["read_receipt_recording_append"] = {
                "event_id": read_receipt_event_id,
                "read_receipt_hash": read_receipt_hash,
            }
    if execute and observer_result.get("status") == "blocked":
        timeout_blocker = _dogfood_cli_timeout_blocker(
            request,
            context=context,
            worker_worktree=worker_worktree,
            owned_files=owned_files,
            observer_result=observer_result,
            executable_worker_launch=executable_worker_launch,
        )
        projection = timeout_blocker["terminal_contract_projection"]
        observer_result["cli_timeout_blocker"] = timeout_blocker
        observer_result["terminal_contract_projection"] = projection
        observer_result["canonical_contract_state"] = projection["canonical_contract_state"]
        observer_result["command_projection_status"] = projection["command_projection_status"]
        observer_result["divergence_reason"] = projection["divergence_reason"]
    invocation = observer_result.get("invocation") or observer_result.get("invocation_request") or {}
    result.update(
        {
            "ok": bool(observer_result.get("ok")),
            "status": observer_result.get("status") or "planned",
            "observer_run": observer_result,
            "planned_invocation": invocation,
            "calls_models": bool(invocation.get("calls_models")),
            "auth_status": invocation.get("auth_status", "not_invoked"),
        }
    )
    if execute and observer_result.get("status") == "blocked":
        timeout_blocker = observer_result.get("cli_timeout_blocker") or {}
        projection = observer_result.get("terminal_contract_projection") or {}
        result["ok"] = False
        result["status"] = "blocked"
        result["calls_models"] = False
        result["cli_timeout_blocker"] = timeout_blocker
        result["terminal_contract_projection"] = projection
        result["canonical_contract_state"] = projection.get("canonical_contract_state", "blocked")
        result["command_projection_status"] = projection.get("command_projection_status", "blocked")
        result["divergence_reason"] = projection.get("divergence_reason", "")
    return result


def run_observer(request: ObserverRunRequest, *, execute: bool = False) -> dict[str, Any]:
    missing = validate_observer_run_request(request)
    invocation_request = build_observer_invocation_request(request)
    if missing:
        return {
            "ok": False,
            "schema_version": OBSERVER_RUN_SCHEMA_VERSION,
            "status": "rejected",
            "missing": missing,
            "execute": execute,
            "invocation_request": invocation_request.to_evidence(),
        }

    if execute:
        execution_gate = validate_one_hop_execution_gate(request)
        if not execution_gate.get("allowed"):
            return {
                "ok": False,
                "schema_version": OBSERVER_RUN_SCHEMA_VERSION,
                "status": "rejected",
                "project_id": request.project_id,
                "backlog_id": request.backlog_id,
                "execute": execute,
                "missing": execution_gate.get("missing") or [],
                "one_hop_execution_gate": execution_gate,
                "invocation_request": invocation_request.to_evidence(),
            }
        result = invoke_ai(invocation_request)
    else:
        execution_gate = {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": _execution_gate_required(request),
            "allowed": True,
            "status": "deferred_until_execute",
        }
        result = AIInvocationResult(
            request=invocation_request,
            status="planned",
            command=[request.backend_mode, "dry-run"],
            returncode=0,
            provider_backed=request.backend_mode != "fixture",
            calls_models=False,
            auth_status="not_invoked",
        )
    evidence = result.to_evidence()
    return {
        "ok": result.status in {"planned", "completed"},
        "schema_version": OBSERVER_RUN_SCHEMA_VERSION,
        "status": result.status,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "execute": execute,
        "one_hop_execution_gate": execution_gate,
        "invocation": evidence,
    }
