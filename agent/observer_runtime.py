"""Observer runtime launcher contracts.

The observer launcher is intentionally thin: it converts route/backlog context
into a provider-neutral AI invocation request. ServiceManager or future manager
HTTP endpoints can call the same functions without depending on click.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field, replace
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
        runtime_context_id_for_branch_context,
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
        runtime_context_id_for_branch_context,
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
WORKER_LAUNCH_BACKENDS = ONE_HOP_REQUIRED_BACKENDS
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
    runtime_context_id: str = ""
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
    runtime_context_id: str = "",
) -> dict[str, Any]:
    supplied = dict(branch_runtime_evidence or {})
    nested = supplied.get("branch_runtime_evidence")
    if isinstance(nested, Mapping):
        supplied = {**supplied, **dict(nested)}
    registration_ref = str(
        branch_runtime_registration_ref
        or supplied.get("registration_ref")
        or supplied.get("source_ref")
        or supplied.get("allocation_source_ref")
        or supplied.get("api_ref")
        or supplied.get("api_route")
        or ""
    ).strip()
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
                value = str(source.get(key) or "").strip()
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
        "parent_task_id": _evidence_field("parent_task_id", "root_task_id", "chain_id"),
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
        "source_ref": registration_ref,
        "registration_ref": registration_ref,
        "runtime_context_id": observed_context["runtime_context_id"],
        "registration_source": str(
            supplied.get("registration_source") or "caller_supplied_allocation_evidence"
        ),
        "allocation_required": False,
        "registered": True,
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


def _runtime_text_context_parent(context: Mapping[str, Any]) -> str:
    return str(
        context.get("parent_task_id")
        or context.get("root_task_id")
        or context.get("chain_id")
        or context.get("backlog_id")
        or ""
    )


def _runtime_text_apply_branch_runtime_context(
    context: Any,
    *,
    parent_task_id: str,
    branch_runtime_registration_ref: str = "",
    branch_runtime_evidence: Mapping[str, Any] | None = None,
    runtime_context_id: str = "",
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
    checks = {
        "task_id": (context.task_id, str(supplied_context.get("task_id") or "")),
        "parent_task_id": (parent_task_id, _runtime_text_context_parent(supplied_context)),
        "fence_token": (context.fence_token, str(supplied_context.get("fence_token") or "")),
        "base_commit": (context.base_commit, str(supplied_context.get("base_commit") or "")),
        "target_head_commit": (
            context.target_head_commit,
            str(supplied_context.get("target_head_commit") or ""),
        ),
        "merge_queue_id": (
            context.merge_queue_id,
            str(supplied_context.get("merge_queue_id") or ""),
        ),
    }
    if any(expected and actual and expected != actual for expected, actual in checks.values()):
        return context

    replacements: dict[str, Any] = {}
    for field_name in (
        "runtime_context_id",
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
        "status",
    ):
        if field_name == "runtime_context_id":
            value = packet.get("runtime_context_id") or supplied_context.get("runtime_context_id")
        else:
            value = supplied_context.get(field_name)
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
        "payload": {
            "mf_subagent_startup_intent": startup_intent,
            "graph_trace_ids": list(graph_trace_ids),
        },
        "artifact_refs": {
            "runtime_context_id": runtime_context_id,
            "launch_text_hash": launch_text_hash,
        },
    }


def _dogfood_host_adapter_startup_evidence(
    request: DogfoodObserverPlanRequest,
    *,
    context: Any,
    runtime_text: Mapping[str, Any],
    worker_worktree: str | Path,
    owned_files: Sequence[str],
) -> dict[str, Any]:
    runtime_context_id = str(
        runtime_text.get("runtime_context_id")
        or request.runtime_context_id
        or runtime_context_id_for_branch_context(context)
    )
    launch_text_hash = str(runtime_text.get("launch_text_hash") or "")
    worker_slot_id = str(
        context.worker_slot_id
        or context.worker_id
        or request.worker_id
        or "dogfood-worker"
    )
    adapter_suffix = _stable_suffix(
        request.project_id,
        request.backlog_id,
        context.task_id,
        worker_slot_id,
        request.backend_mode,
        runtime_context_id,
        launch_text_hash,
    )
    agent_id = f"{request.backend_mode}-host-adapter-{adapter_suffix}"
    actual_host_worker_id = f"{request.backend_mode}-host-worker-{adapter_suffix}"
    head_commit = _git_head(Path(worker_worktree)) or context.head_commit or context.target_head_commit
    branch = _git_current_branch(worker_worktree) or context.branch_ref
    try:
        actual_git_root = str(batch_jobs.repo_root(worker_worktree))
    except Exception:
        actual_git_root = str(Path(str(worker_worktree)).expanduser().resolve())
    session_token_surrogate = (
        "host-adapter:"
        + _stable_suffix(
            request.route_id,
            runtime_context_id,
            context.task_id,
            worker_slot_id,
            launch_text_hash,
            length=24,
        )
    )
    observer_command_id = request.task_id or context.task_id
    read_receipt_material = {
        "schema_version": "mf_subagent_read_receipt.v1",
        "observer_command_id": observer_command_id,
        "runtime_context_id": runtime_context_id,
        "task_id": context.task_id,
        "route_id": request.route_id,
        "worker_id": worker_slot_id,
        "worker_slot_id": worker_slot_id,
        "actual_host_worker_id": actual_host_worker_id,
        "agent_id": agent_id,
        "fence_token": context.fence_token,
        "branch": branch,
        "branch_ref": context.branch_ref,
        "worktree": str(Path(str(worker_worktree)).expanduser().resolve()),
        "worktree_path": str(Path(str(worker_worktree)).expanduser().resolve()),
        "owned_files": list(owned_files),
        "launch_text_hash": launch_text_hash,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
    }
    read_receipt_hash = "sha256:" + hashlib.sha256(
        json.dumps(read_receipt_material, sort_keys=True).encode("utf-8")
    ).hexdigest()
    agent_id_match_mode = (
        "exact_or_unallocated"
        if not (context.allocation_owner or context.agent_id)
        or (context.allocation_owner or context.agent_id) == agent_id
        else "host_adapter_startup_token_surrogate"
    )
    startup_gate = {
        "schema_version": "mf_subagent_startup_gate.v1",
        "gate_kind": "mf_subagent.startup",
        "status": "prepared",
        "ok": True,
        "allowed": True,
        "bounded": True,
        "started": False,
        "startup_complete": False,
        "actual_startup_recorded": False,
        "actual_startup_prepared": True,
        "actual_startup_appendable": True,
        "actual_startup_required": True,
        "timeline_event_recorded": False,
        "same_as_expected_worker": actual_host_worker_id == worker_slot_id,
        "fence_token_matches": True,
        "close_satisfying": False,
        "raw_launch_text_persisted": False,
        "startup_evidence_kind": "prepared_appendable",
        "durable_recording_surface": (
            f"POST /api/graph-governance/{request.project_id}/parallel-branches/startup"
        ),
        "project_id": request.project_id,
        "governance_project_id": context.governance_project_id or request.project_id,
        "target_project_id": context.target_project_id or request.project_id,
        "target_project_root": context.target_project_root,
        "backlog_id": request.backlog_id,
        "runtime_context_id": runtime_context_id,
        "task_id": context.task_id,
        "parent_task_id": request.backlog_id,
        "worker_role": "mf_sub",
        "role": "mf_sub",
        "allocation_owner": context.allocation_owner or context.agent_id,
        "observer_allocation_owner": context.allocation_owner or context.agent_id,
        "worker_id": worker_slot_id,
        "worker_slot_id": worker_slot_id,
        "actual_host_worker_id": actual_host_worker_id,
        "agent_id": agent_id,
        "expected_agent_id": context.allocation_owner or context.agent_id,
        "agent_id_match_mode": agent_id_match_mode,
        "host_adapter_startup_token_accepted": True,
        "fence_token": context.fence_token,
        "branch": branch,
        "branch_ref": context.branch_ref,
        "worktree": str(Path(str(worker_worktree)).expanduser().resolve()),
        "worktree_path": str(Path(str(worker_worktree)).expanduser().resolve()),
        "assigned_worktree": context.worktree_path,
        "actual_cwd": str(Path(str(worker_worktree)).expanduser().resolve()),
        "actual_git_root": actual_git_root,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "head_commit": head_commit,
        "merge_queue_id": context.merge_queue_id,
        "owned_files": list(owned_files),
        "route_id": request.route_id,
        "precheck_run_id": request.precheck_run_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "observer_command_id": observer_command_id,
        "session_token_hash": "",
        "session_token_surrogate": session_token_surrogate,
        "session_token_evidence_type": "surrogate",
        "session_token_present": False,
        "session_token_persisted": False,
        "startup_source": f"{request.backend_mode}_host_adapter",
        "startup_timing": "prepared_before_implementation_wait",
        "launch_text_hash": launch_text_hash,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt": read_receipt_material,
    }
    startup_recording = {
        **startup_gate,
        "recorded": False,
        "prepared": True,
        "appendable": True,
        "timeline_event_recorded": False,
        "append_tool": "parallel_branch_startup",
        "event_kind": "mf_subagent_startup",
    }
    read_receipt = {
        **read_receipt_material,
        "hash": read_receipt_hash,
        "read_receipt_hash": read_receipt_hash,
        "recorded": False,
        "appendable": True,
        "prepared_before_implementation_wait": True,
        "recorded_before_implementation_wait": False,
    }
    startup_event = {
        "schema_version": 2,
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "prepared",
        "actor": "mf_sub",
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "task_id": context.task_id,
        "attempt_num": context.attempt,
        "correlation_id": f"host-adapter-startup-{adapter_suffix}",
        "payload": {
            "mf_subagent_startup_gate": startup_gate,
            "read_receipt": read_receipt,
        },
        "artifact_refs": {
            "runtime_context_id": runtime_context_id,
            "session_token_evidence_type": "surrogate",
            "read_receipt_hash": read_receipt_hash,
            "startup_evidence_kind": "prepared_appendable",
            "timeline_event_recorded": False,
        },
        "commit_sha": head_commit,
    }
    return {
        "startup_recording": startup_recording,
        "startup_timeline_event": startup_event,
        "read_receipt": read_receipt,
        "actual_startup_recorded": False,
        "startup_evidence_kind": "prepared_appendable",
        "startup_evidence_appendable": True,
        "timeline_event_recorded": False,
    }


def _dogfood_cli_timeout_blocker(
    request: DogfoodObserverPlanRequest,
    *,
    context: Any,
    worker_worktree: str | Path,
    owned_files: Sequence[str],
    observer_result: Mapping[str, Any],
) -> dict[str, Any]:
    invocation = observer_result.get("invocation") if isinstance(observer_result, Mapping) else {}
    invocation = invocation if isinstance(invocation, Mapping) else {}
    diff_scope = _worker_worktree_diff_scope(
        worktree=worker_worktree,
        base_commit=context.base_commit,
        owned_files=owned_files,
    )
    no_output = bool(invocation.get("output_empty", True))
    observer_command_id = request.task_id or context.task_id
    blocker_id = (
        f"{request.backend_mode}_timeout_no_output_no_finish"
        if no_output
        else f"{request.backend_mode}_timeout_no_finish"
    )
    terminal_projection = {
        "schema_version": "observer_command_terminal_projection.v1",
        "passed": False,
        "canonical_contract_state": "blocked",
        "command_projection_status": "failed",
        "divergence_reason": blocker_id,
        "terminal_evidence_refs": [
            "mf_subagent_startup_prepared",
            "read_receipt_prepared",
            "cli_timeout_blocker",
            "worktree_diff_scope",
        ],
        "canonical_route_identity": {
            "route_id": request.route_id,
            "route_context_hash": request.route.route_context_hash,
            "prompt_contract_id": request.route.prompt_contract_id,
            "prompt_contract_hash": request.route.prompt_contract_hash,
            "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        },
        "observer_command_id": observer_command_id,
    }
    return {
        "schema_version": "observer_cli_timeout_blocker.v1",
        "blocker_id": blocker_id,
        "terminal_blocker": True,
        "terminal_dispatch_blocker": True,
        "status": "blocked",
        "observer_command_id": observer_command_id,
        "task_id": context.task_id,
        "route_id": request.route_id,
        "route_context_hash": request.route.route_context_hash,
        "prompt_contract_id": request.route.prompt_contract_id,
        "prompt_contract_hash": request.route.prompt_contract_hash,
        "visible_injection_manifest_hash": request.visible_injection_manifest_hash,
        "branch": context.branch_ref,
        "worktree": str(Path(str(worker_worktree)).expanduser().resolve()),
        "fence_token": context.fence_token,
        "owned_files": list(owned_files),
        "backend_mode": request.backend_mode,
        "timeout_sec": request.timeout_sec,
        "no_output": no_output,
        "no_finish_evidence": True,
        "startup_recorded": False,
        "read_receipt_recorded": False,
        "read_receipt_recorded_before_implementation_wait": False,
        "failure_evidence_appended": True,
        "command_projection_status": "failed",
        "calls_models": False,
        "auth_status": invocation.get("auth_status", "cli_timeout"),
        "worktree_diff_scope": diff_scope,
        "terminal_contract_projection": terminal_projection,
        "reason": (
            "CLI backend timed out before finish evidence; worker startup/read receipt "
            "evidence was prepared for append, and the isolated worktree diff scope is reported."
        ),
    }


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
        "Before graph query, startup, implementation, verification, or close-ready "
        "evidence can satisfy close-sensitive gates, record an "
        "mf_subagent_read_receipt for this visible route contract. A post-hoc "
        "read receipt after counted evidence does not satisfy the ordering gate.\n\n"
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
    context = plan_branch_runtime_context(
        project_id=request.project_id,
        task_id=task_id,
        workspace_root=str(workspace_root),
        backlog_id=request.backlog_id,
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
        branch_runtime_evidence=request.branch_runtime_evidence,
        runtime_context_id=request.runtime_context_id,
    )
    owned_files = _runtime_text_items(request.owned_files)
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
        governance_project_id=context.governance_project_id or request.project_id,
        target_project_id=context.target_project_id or request.project_id,
        target_project_root=context.target_project_root,
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
    startup_intent_event = (
        _runtime_text_startup_intent_event(
            request=request,
            runtime_context_id=runtime_context_id,
            context=context,
            parent_task_id=parent_task_id,
            launch_text_hash=launch_text_hash,
            graph_trace_ids=graph_trace_ids,
        )
        if ok
        else {}
    )
    startup_recording = {
        "schema_version": "mf_subagent_startup_recording.v1",
        "required": bool(ok),
        "recorded": False,
        "close_ready": False,
        "append_tool": "task_timeline_append",
        "event_kind": "mf_subagent_startup",
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
    dispatch_gate_validation = {
        **dispatch_gate_validation,
        "startup_intent_event_generated": bool(ok),
        "actual_startup_required": bool(ok),
        "actual_startup_recorded": False,
        "close_ready": False,
    }
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
            "startup_intent_event_generated": bool(ok),
            "actual_startup_required": bool(ok),
            "actual_startup_recorded": False,
            "close_ready": False,
            "startup_recording": startup_recording,
            "startup_intent_event": startup_intent_event,
        },
        "startup_intent_event": startup_intent_event,
        "startup_recording": startup_recording,
        "close_ready": False,
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
    owned_files = [str(item) for item in request.owned_files if str(item or "").strip()]
    graph_trace_ids = [
        str(item) for item in request.graph_trace_ids if str(item or "").strip()
    ]
    parent_task_id = request.backlog_id
    runtime_text_request = ObserverRuntimeTextPrepareRequest(
        project_id=request.project_id,
        backlog_id=request.backlog_id,
        route=request.route,
        governance_project_id=governance_project_id,
        target_project_id=target_project_id,
        target_project_root=request.target_project_root,
        allocation_owner=allocation_owner,
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
        runtime_context_id=request.runtime_context_id,
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

    startup_evidence: dict[str, Any] = {}
    if execute:
        startup_evidence = _dogfood_host_adapter_startup_evidence(
            request,
            context=context,
            runtime_text=runtime_text,
            worker_worktree=worker_worktree,
            owned_files=owned_files,
        )
        result.update(startup_evidence)
        result["dispatch_gate_validation"] = {
            **result["dispatch_gate_validation"],
            "actual_startup_recorded": False,
            "startup_evidence": "mf_subagent_startup_prepared",
            "startup_evidence_appendable": True,
            "timeline_event_recorded": False,
            "read_receipt_hash": startup_evidence["read_receipt"]["read_receipt_hash"],
        }

    observer_result = run_observer(observer_request, execute=execute)
    if startup_evidence:
        observer_result.update(startup_evidence)
    if execute and observer_result.get("status") == "blocked":
        timeout_blocker = _dogfood_cli_timeout_blocker(
            request,
            context=context,
            worker_worktree=worker_worktree,
            owned_files=owned_files,
            observer_result=observer_result,
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
