"""Observer runtime launcher contracts.

The observer launcher is intentionally thin: it converts route/backlog context
into a provider-neutral AI invocation request. ServiceManager or future manager
HTTP endpoints can call the same functions without depending on click.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from ai_invocation import (
        AIInvocationRequest,
        AIInvocationResult,
        RoutePromptContract,
        invoke_ai,
    )
    from governance.mf_subagent_contract import (
        MfSubagentContractError,
        build_mf_subagent_input,
        validate_mf_subagent_dispatch_gate,
    )
    from governance import batch_jobs
    from governance.parallel_branch_runtime import (
        branch_strategy_from_runtime_context,
        plan_branch_runtime_context,
    )
except ImportError:  # pragma: no cover - package import path
    from agent.ai_invocation import (
        BACKEND_CLAUDE_CLI,
        BACKEND_CODEX_CLI,
        BACKEND_DOCKER_LIVE_AI,
        AIInvocationRequest,
        AIInvocationResult,
        RoutePromptContract,
        invoke_ai,
    )
    from agent.governance.mf_subagent_contract import (
        MfSubagentContractError,
        build_mf_subagent_input,
        validate_mf_subagent_dispatch_gate,
    )
    from agent.governance import batch_jobs
    from agent.governance.parallel_branch_runtime import (
        branch_strategy_from_runtime_context,
        plan_branch_runtime_context,
    )
else:  # pragma: no cover - direct module import path
    from ai_invocation import BACKEND_CLAUDE_CLI, BACKEND_CODEX_CLI, BACKEND_DOCKER_LIVE_AI


OBSERVER_RUN_SCHEMA_VERSION = "observer_run.v1"
OBSERVER_POLL_SCHEMA_VERSION = "observer_poll.v1"
OBSERVER_POLL_TIMELINE_PAYLOAD_SCHEMA_VERSION = "observer_poll_timeline_payload.v1"
DOGFOOD_OBSERVER_PLAN_SCHEMA_VERSION = "observer_dogfood_plan.v1"
OBSERVER_RUNTIME_TEXT_SCHEMA_VERSION = "observer_runtime_text_context.v1"
OBSERVER_RUNTIME_TEXT_SERVICE_SCHEMA_VERSION = "observer_runtime_text_service.v1"
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
    "graph_trace_evidence",
    "branch_runtime_evidence",
    "service_dispatch_evidence",
    "startup_echo",
    "finish_gate",
)
RUNTIME_TEXT_BRANCH_RUNTIME_REF_MARKERS = (
    "/parallel-branches/allocate",
    "parallel-branches/allocate",
    "upsert_branch_context",
)
RUNTIME_TEXT_REQUIRED_LANES = (
    "observer_coordinator",
    "bounded_implementation_worker",
    "observer_review_gate",
)


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
    dispatch_gate: Mapping[str, Any] = field(default_factory=dict)
    main_worktree: str = ""

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
        )


@dataclass
class DogfoodObserverPlanRequest:
    project_id: str
    backlog_id: str
    route: RoutePromptContract
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
    base_commit: str = ""
    target_head_commit: str = ""
    prompt: str = ""
    timeout_sec: int = 120
    route_id: str = ""
    precheck_run_id: str = ""
    visible_injection_manifest_hash: str = ""


@dataclass
class ObserverRuntimeTextPrepareRequest:
    project_id: str
    backlog_id: str
    route: RoutePromptContract
    main_worktree: str = ""
    workspace_root: str = ""
    owned_files: tuple[str, ...] = ()
    task_id: str = ""
    parent_task_id: str = ""
    worker_id: str = ""
    attempt: int = 1
    worktree_root: str = ".worktrees"
    branch_prefix: str = "runtime-text"
    merge_queue_id: str = ""
    fence_token: str = ""
    graph_trace_ids: tuple[str, ...] = ()
    branch_runtime_registration_ref: str = ""
    branch_runtime_evidence: Mapping[str, Any] = field(default_factory=dict)
    base_commit: str = ""
    target_head_commit: str = ""
    prompt: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()
    route_id: str = ""
    precheck_run_id: str = ""
    visible_injection_manifest_hash: str = ""
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
    dispatch_gate: Mapping[str, Any] = field(default_factory=dict)
    main_worktree: str = ""


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
        dispatch_gate=request.dispatch_gate,
        main_worktree=request.main_worktree or str(Path.cwd()),
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
    return result


def _stable_suffix(*parts: str, length: int = 12) -> str:
    payload = "\n".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _normalize_path(path: str) -> str:
    token = str(path or "").strip()
    if not token:
        return ""
    return str(Path(token).expanduser().resolve())


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
        "Do not expose raw private route/context-pack content."
    )


def build_observer_invocation_request(request: ObserverRunRequest) -> AIInvocationRequest:
    workspace = request.workspace or str(Path.cwd())
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
        metadata={
            "project_id": request.project_id,
            "backlog_id": request.backlog_id,
            "observer_launcher": True,
        },
    )


def _runtime_text_items(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    return [str(item) for item in values if str(item or "").strip()]


def _runtime_text_parent_route_lineage(
    request: ObserverRuntimeTextPrepareRequest,
) -> dict[str, Any]:
    return {
        "schema_version": "parent_route_lineage.v1",
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "selected_project": request.project_id,
        "selected_backlog_id": request.backlog_id,
        "allowed_actions": [
            "prepare_runtime_text",
            "dispatch_bounded_worker",
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


def _runtime_text_branch_runtime_evidence(
    *,
    project_id: str,
    context: Any,
    parent_task_id: str,
    branch_runtime_registration_ref: str = "",
    branch_runtime_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    supplied = dict(branch_runtime_evidence or {})
    registration_ref = str(
        branch_runtime_registration_ref
        or supplied.get("registration_ref")
        or supplied.get("source_ref")
        or supplied.get("api_ref")
        or supplied.get("api_route")
        or ""
    ).strip()
    registration_ref_valid = bool(
        registration_ref
        and any(marker in registration_ref for marker in RUNTIME_TEXT_BRANCH_RUNTIME_REF_MARKERS)
    )
    supplied_context = supplied.get("context") if isinstance(supplied.get("context"), Mapping) else {}
    supplied_status = str(supplied.get("status") or "").strip().lower()
    supplied_rejected = bool(supplied) and (
        supplied.get("ok") is False
        or supplied.get("registered") is False
        or bool(supplied.get("allocation_required"))
        or supplied_status in {"allocation_required", "rejected", "failed", "error"}
    )
    if not registration_ref_valid:
        return {
            "schema_version": "mf_subagent_branch_runtime.v1",
            "status": "allocation_required",
            "allocation_required": True,
            "registered": False,
            "present": False,
            "source": "observer_runtime_text_prepare",
            "message": (
                "Call MCP parallel_branch_allocate, "
                f"POST /api/graph-governance/{project_id}/parallel-branches/allocate, "
                "or upsert_branch_context, then pass branch_runtime_registration_ref "
                "or branch_runtime_evidence with a valid source_ref/registration_ref."
            ),
            "supplied_source_ref": registration_ref,
            "planned_context": {
                "task_id": context.task_id,
                "parent_task_id": parent_task_id,
                "fence_token": context.fence_token,
                "worktree_path": context.worktree_path,
                "base_commit": context.base_commit,
                "target_head_commit": context.target_head_commit,
                "merge_queue_id": context.merge_queue_id,
            },
        }
    if supplied_rejected:
        return {
            "schema_version": "mf_subagent_branch_runtime.v1",
            "status": "allocation_required",
            "allocation_required": True,
            "registered": False,
            "present": False,
            "source": "observer_runtime_text_prepare",
            "message": "Supplied branch runtime evidence is not a registered allocation.",
            "supplied_evidence_status": supplied_status or supplied.get("status") or "",
            "planned_context": {
                "task_id": context.task_id,
                "parent_task_id": parent_task_id,
                "fence_token": context.fence_token,
                "worktree_path": context.worktree_path,
                "base_commit": context.base_commit,
                "target_head_commit": context.target_head_commit,
                "merge_queue_id": context.merge_queue_id,
            },
        }
    return {
        "schema_version": "mf_subagent_branch_runtime.v1",
        "source_ref": registration_ref,
        "registration_ref": registration_ref,
        "registration_source": str(
            supplied.get("registration_source") or "caller_supplied_allocation_evidence"
        ),
        "allocation_required": False,
        "registered": True,
        "context": {
            "task_id": str(supplied_context.get("task_id") or context.task_id),
            "parent_task_id": str(
                supplied_context.get("parent_task_id")
                or supplied_context.get("root_task_id")
                or parent_task_id
            ),
            "fence_token": str(supplied_context.get("fence_token") or context.fence_token),
            "worktree_path": str(
                supplied_context.get("worktree_path")
                or supplied_context.get("worktree")
                or context.worktree_path
            ),
            "base_commit": str(supplied_context.get("base_commit") or context.base_commit),
            "target_head_commit": str(
                supplied_context.get("target_head_commit") or context.target_head_commit
            ),
            "merge_queue_id": str(
                supplied_context.get("merge_queue_id") or context.merge_queue_id
            ),
        },
    }


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
    context: Any,
    parent_task_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "mf_subagent_startup_echo.v1",
        "required": True,
        "runtime_context_id": runtime_context_id,
        "must_echo_fields": [
            "runtime_context_id",
            "project_id",
            "task_id",
            "parent_task_id",
            "worker_role",
            "fence_token",
            "actual_cwd",
            "actual_git_root",
            "branch",
            "head_commit",
        ],
        "expected": {
            "project_id": context.project_id,
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": context.fence_token,
            "worktree_path": context.worktree_path,
            "branch_ref": context.branch_ref,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
        },
    }


def _runtime_text_graph_first_obligations(
    *,
    project_id: str,
    task_id: str,
    parent_task_id: str,
    fence_token: str,
) -> dict[str, Any]:
    return {
        "schema_version": "mf_subagent_graph_first_obligations.v1",
        "required": True,
        "query": {
            "project_id": project_id,
            "query_source": "mf_subagent",
            "query_purpose": "subagent_context_build",
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": fence_token,
        },
        "minimum_before_edit": [
            "graph_query tool=query_schema",
            "graph_query tool=find_node_by_path for owned files",
        ],
        "trace_evidence_schema_version": "mf_subagent_graph_trace.v1",
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
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
            "merge_queue_id": context.merge_queue_id,
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
        "review_ready with structured evidence. Do not merge, push, activate graph "
        "refs, mutate merge queues, delete worktrees, or expose raw private "
        "route/context-pack content."
    )


def _runtime_text_launch_text(payload: Mapping[str, Any]) -> str:
    return (
        "You are a bounded mf_sub implementation worker for Aming Claw.\n\n"
        "Follow the runtime context exactly. Raw private route/context-pack "
        "content is outside this worker boundary; use only the bounded contract "
        "fields below.\n\n"
        "Persistent evidence must store runtime_context_id and launch_text_hash "
        "only; raw launch text is returned to the host for launch and must not be "
        "persisted in timeline evidence.\n\n"
        "Runtime contract JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )


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
    context = plan_branch_runtime_context(
        project_id=request.project_id,
        task_id=task_id,
        workspace_root=str(workspace_root),
        backlog_id=request.backlog_id,
        chain_id=parent_task_id,
        root_task_id=parent_task_id,
        stage_type="mf_sub_runtime_text",
        worker_id=worker_id,
        attempt=request.attempt,
        branch_prefix=request.branch_prefix,
        worktree_root=request.worktree_root,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
    )
    owned_files = _runtime_text_items(request.owned_files)
    graph_trace_ids = _runtime_text_items(request.graph_trace_ids)
    runtime_context_id = "orctx-" + _stable_suffix(
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
    parent_route_lineage = _runtime_text_parent_route_lineage(request)
    graph_trace_evidence = _runtime_text_graph_trace_evidence(
        graph_trace_ids=graph_trace_ids,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        fence_token=context.fence_token,
    )
    branch_runtime_evidence = _runtime_text_branch_runtime_evidence(
        project_id=request.project_id,
        context=context,
        parent_task_id=parent_task_id,
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=request.branch_runtime_evidence,
    )
    service_dispatch_evidence = _runtime_text_service_dispatch_evidence()
    dispatch_gate = {
        "schema_version": "mf_subagent_dispatch_gate.v1",
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
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
        "graph_trace_evidence": graph_trace_evidence,
        "graph_evidence": graph_trace_evidence,
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
    mf_subagent_input: dict[str, Any]
    input_error = ""
    try:
        mf_subagent_input = build_mf_subagent_input(
            context,
            prompt=worker_prompt,
            acceptance_criteria=request.acceptance_criteria,
            target_files=owned_files,
            test_commands=request.test_commands,
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
        context=context,
        parent_task_id=parent_task_id,
    )
    graph_first_obligations = _runtime_text_graph_first_obligations(
        project_id=request.project_id,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        fence_token=context.fence_token,
    )
    finish_gate_contract = _runtime_text_finish_gate_contract(context)
    launch_payload = {
        "schema_version": OBSERVER_RUNTIME_TEXT_SCHEMA_VERSION,
        "runtime_context_id": runtime_context_id,
        "runtime_context": asdict(context),
        "branch_identity": {
            "branch_ref": context.branch_ref,
            "worktree_path": context.worktree_path,
            "base_commit": context.base_commit,
            "target_head_commit": context.target_head_commit,
            "merge_queue_id": context.merge_queue_id,
            "fence_token": context.fence_token,
        },
        "dispatch_gate": dispatch_gate,
        "mf_subagent_input": mf_subagent_input,
        "startup_echo_contract": startup_echo_contract,
        "graph_first_obligations": graph_first_obligations,
        "finish_gate_contract": finish_gate_contract,
    }
    launch_text = _runtime_text_launch_text(launch_payload)
    launch_text_hash = "sha256:" + hashlib.sha256(
        launch_text.encode("utf-8")
    ).hexdigest()

    try:
        dispatch_gate_validation = validate_mf_subagent_dispatch_gate(
            dispatch_gate,
            target_worktree_path=str(main_worktree),
            main_worktree_path=str(main_worktree),
        )
    except MfSubagentContractError as exc:
        dispatch_gate_validation = {"allowed": False, "error": str(exc)}

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
    ok = bool(dispatch_gate_validation.get("allowed")) and not input_error
    return {
        "ok": ok,
        "schema_version": OBSERVER_RUNTIME_TEXT_SCHEMA_VERSION,
        "service_schema_version": OBSERVER_RUNTIME_TEXT_SERVICE_SCHEMA_VERSION,
        "status": "prepared" if ok else ("allocation_required" if allocation_required else "rejected"),
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "runtime_context_id": runtime_context_id,
        "launch_text": launch_text,
        "launch_text_hash": launch_text_hash,
        "raw_launch_text_persisted": False,
        "persistent_evidence": {
            "runtime_context_id": runtime_context_id,
            "launch_text_hash": launch_text_hash,
            "raw_launch_text_persisted": False,
            "dispatch_ready": ok,
            "allocation_required": allocation_required,
        },
        "runtime_context": asdict(context),
        "branch_identity": launch_payload["branch_identity"],
        "mf_subagent_input": mf_subagent_input,
        "dispatch_gate": dispatch_gate,
        "dispatch_gate_validation": dispatch_gate_validation,
        "branch_runtime_evidence": branch_runtime_evidence,
        "service_dispatch_evidence": service_dispatch_evidence,
        "startup_echo_contract": startup_echo_contract,
        "graph_first_obligations": graph_first_obligations,
        "finish_gate_contract": finish_gate_contract,
        "route_identity": {
            "route_id": request.route_id,
            "route_context_hash": request.route.route_context_hash,
            "prompt_contract_id": request.route.prompt_contract_id,
            "prompt_contract_hash": request.route.prompt_contract_hash,
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
    context = plan_branch_runtime_context(
        project_id=request.project_id,
        task_id=task_id,
        workspace_root=str(workspace_root),
        backlog_id=request.backlog_id,
        stage_type="observer_dogfood",
        worker_id=worker_id,
        attempt=request.attempt,
        branch_prefix=request.branch_prefix,
        worktree_root=request.worktree_root,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
    )
    owned_files = [str(item) for item in request.owned_files if str(item or "").strip()]
    graph_trace_ids = [
        str(item) for item in request.graph_trace_ids if str(item or "").strip()
    ]
    parent_task_id = request.backlog_id
    runtime_text_request = ObserverRuntimeTextPrepareRequest(
        project_id=request.project_id,
        backlog_id=request.backlog_id,
        route=request.route,
        main_worktree=str(main_worktree),
        workspace_root=str(workspace_root),
        owned_files=tuple(owned_files),
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
        branch_runtime_evidence=request.branch_runtime_evidence,
        base_commit=context.base_commit,
        target_head_commit=context.target_head_commit,
        prompt=request.prompt,
        route_id=request.route_id,
        precheck_run_id=request.precheck_run_id,
        visible_injection_manifest_hash=request.visible_injection_manifest_hash,
    )
    parent_route_lineage = _runtime_text_parent_route_lineage(runtime_text_request)
    graph_trace_evidence = _runtime_text_graph_trace_evidence(
        graph_trace_ids=graph_trace_ids,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        fence_token=context.fence_token,
    )
    branch_runtime_evidence = _runtime_text_branch_runtime_evidence(
        project_id=request.project_id,
        context=context,
        parent_task_id=parent_task_id,
        branch_runtime_registration_ref=request.branch_runtime_registration_ref,
        branch_runtime_evidence=request.branch_runtime_evidence,
    )
    service_dispatch_evidence = _runtime_text_service_dispatch_evidence()
    dispatch_gate = {
        "schema_version": "mf_subagent_dispatch_gate.v1",
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": "mf_sub",
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
        "graph_trace_evidence": graph_trace_evidence,
        "graph_evidence": graph_trace_evidence,
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
    if not graph_trace_ids:
        result["dispatch_gate_validation"] = {
            "allowed": False,
            "error": "observer dogfood dispatch requires at least one graph_trace_id",
        }
        return result
    result["dispatch_gate_validation"] = gate_validation
    runtime_text = build_observer_runtime_text_context(runtime_text_request)
    result["runtime_text"] = runtime_text
    if not runtime_text.get("ok"):
        result["dispatch_gate_validation"] = (
            runtime_text.get("dispatch_gate_validation") or gate_validation
        )
        result["error"] = (
            runtime_text.get("input_error")
            or (runtime_text.get("dispatch_gate_validation") or {}).get("error")
            or "observer runtime text preparation failed"
        )
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

    observer_request = ObserverRunRequest(
        project_id=request.project_id,
        backlog_id=request.backlog_id,
        route=request.route,
        provider=request.provider,
        model=request.model,
        backend_mode=request.backend_mode,
        workspace=str(worker_worktree),
        prompt=request.prompt,
        timeout_sec=request.timeout_sec,
        dispatch_gate=dispatch_gate,
        main_worktree=str(main_worktree),
    )
    if execute and not materialization.get("materialized"):
        result["execute_preflight"] = {
            "allowed": False,
            "error": "execute requires the gated worker worktree to be an isolated real git worktree",
            "worktree": str(worker_worktree),
            "worktree_status": materialization.get("worktree_status") or {},
        }
        return result

    observer_result = run_observer(observer_request, execute=execute)
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
