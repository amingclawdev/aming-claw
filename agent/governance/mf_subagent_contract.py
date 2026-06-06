"""Backend-neutral contract for MF subagent branch workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from agent.governance.asset_binding_proposals import (
    PRECHECK_SCHEMA_VERSION as ASSET_BINDING_PRECHECK_SCHEMA_VERSION,
    PROPOSAL_SCHEMA_VERSION as ASSET_BINDING_PROPOSAL_SCHEMA_VERSION,
)
from agent.governance.parallel_branch_runtime import (
    BranchRuntimeContractRevision,
    BranchTaskRuntimeContext,
    RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION,
    RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION,
    build_runtime_context_projection,
    runtime_context_id_for_branch_context,
)


MF_SUB_ROLE = "mf_sub"
OBSERVER_COORDINATOR_ROLE = "observer"
INPUT_SCHEMA_VERSION = "mf_subagent_input.v1"
RESULT_SCHEMA_VERSION = "mf_subagent_result.v1"
FINISH_GATE_SCHEMA_VERSION = "mf_subagent_finish_gate.v1"
DISPATCH_GATE_SCHEMA_VERSION = "mf_subagent_dispatch_gate.v1"
RUNTIME_CONTRACT_VIEW_SCHEMA_VERSION = "mf_subagent_runtime_contract_view.v1"
AGENT_TASK_CONTRACT_SCHEMA_VERSION = "observer_owned_agent_task_contract.v1"
AGENT_TASK_CONTRACT_PROJECTION_SCHEMA_VERSION = "agent_task_contract_projection.v1"
VERIFICATION_ROUTE_POLICY_SCHEMA_VERSION = "verification_route_policy.v1"
PARENT_ROUTE_LINEAGE_SCHEMA_VERSION = "parent_route_lineage.v1"
ROUTE_LINEAGE_SCHEMA_VERSION = "mf_subagent_route_lineage.v1"
GRAPH_TRACE_SCHEMA_VERSION = "mf_subagent_graph_trace.v1"
SERVICE_DISPATCH_SCHEMA_VERSION = "observer_subagent_service_dispatch.v1"
BRANCH_RUNTIME_SCHEMA_VERSION = "mf_subagent_branch_runtime.v1"
OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION = "observer_direct_mutation_exception.v1"
ROUTE_ACTION_GATE_SCHEMA_VERSION = "route_action_gate.v1"
ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION = "route_token_mutation_gate.v1"
ROUTE_TOKEN_REQUIRED_FAILURE_SCHEMA_VERSION = "route_token_required_failure.v1"
FINISH_GATE_REPLAY_SOURCE = "mf_sub_finish_gate"
BACKEND_CONTRACT = "parallel_branch_worker.v1"
DISPATCH_DEFAULT = "non_blocking_after_gate"
WORKTREE_POLICY_MODE = "isolated_worktree_required"
OBSERVER_DIRECT_MUTATION_DEFAULT = "reject"
ROUTE_OBSERVER_JUDGER_BLOCK_ALERT = "observer_judger_must_not_implement"
ROUTE_OBSERVER_INDEPENDENT_REVIEWER_BLOCK_ALERT = (
    "observer_independent_reviewer_must_not_implement"
)
ROUTE_DIRECT_IMPLEMENTATION_BLOCK_ALERTS = {
    ROUTE_OBSERVER_JUDGER_BLOCK_ALERT,
    ROUTE_OBSERVER_INDEPENDENT_REVIEWER_BLOCK_ALERT,
}

MF_SUB_ALLOWED_CAPABILITIES = (
    "modify_code",
    "run_tests",
    "git_diff",
    "checkpoint_branch_task",
    "report_blocker",
)
MF_SUB_FORBIDDEN_ACTIONS = (
    "merge",
    "push",
    "activate_graph",
    "release_gate",
    "create_task",
    "delete_worktree",
    "modify_merge_queue",
)
MF_SUB_REQUIRED_OUTPUT = (
    "status",
    "changed_files",
    "test_results",
    "checkpoint_id",
    "fence_token",
)

_REQUIRED_CONTEXT_FIELDS = (
    "project_id",
    "task_id",
    "backlog_id",
    "branch_ref",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "fence_token",
)
_READY_STATUSES = {
    "completed",
    "succeeded",
    "ready_for_merge",
    "review_ready",
    "waiting_merge",
}
_PASS_STATUSES = {"pass", "passed", "ok", "succeeded", "success", "clean"}
_FAIL_STATUSES = {
    "block",
    "blocked",
    "deny",
    "denied",
    "error",
    "errored",
    "fail",
    "failed",
    "not_allowed",
    "reject",
    "rejected",
}
_FORBIDDEN_RESULT_FLAGS = {
    "merge_commit": "merge",
    "push_performed": "push",
    "graph_activated": "activate_graph",
    "release_gate_passed": "release_gate",
    "task_created": "create_task",
    "worktree_deleted": "delete_worktree",
}
_FINISH_IDENTITY_FIELDS = (
    "project_id",
    "task_id",
    "backlog_id",
    "branch_ref",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
)
_DISPATCH_REQUIRED_FIELDS = (
    "branch",
    "worktree",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "fence_token",
    "route_context_hash",
    "prompt_contract_id",
)
_PARENT_ROUTE_LINEAGE_REQUIRED_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_hash",
    "selected_project",
    "selected_backlog_id",
    "allowed_actions",
    "blocked_actions",
    "required_lanes",
    "required_evidence",
)
_PARENT_ROUTE_LINEAGE_KEYS = (
    "parent_route_lineage",
    "parent_route_identity",
    "parent_judgment_route",
    "parent_judge_route",
    "judgment_route",
    "judgment_route_identity",
    "judge_route",
    "judge_route_identity",
    "parent_route",
    "parent_route_context",
)
_PARENT_ROUTE_LINEAGE_CONTAINER_KEYS = (
    "route_lineage",
    "lineage",
    "dispatch",
    "dispatch_context",
    "dispatch_gate",
    "worker_dispatch",
    "worker_contract",
    "bounded_dispatch",
    "route_context",
    "route_prompt_bundle",
    "bundle",
    "evidence",
    "verification",
    "artifact_refs",
)
_PARENT_ROUTE_REQUIRED_KEYS = (
    "parent_route_required",
    "parent_lineage_required",
    "route_lineage_required",
    "judge_routed",
    "judgment_routed",
    "parent_judge_routed",
    "parent_route_lineage_required",
)
_PARENT_ROUTE_LINEAGE_MARKER_KEYS = (
    "parent_route_id",
    "judge_route_id",
    "judgment_route_id",
    "parent_route_context_hash",
    "parent_prompt_contract_id",
    "parent_visible_injection_manifest_hash",
)
_GOVERNED_NONTRIVIAL_KEYS = (
    "governed_nontrivial",
    "governed_nontrivial_work",
    "governed_nontrivial_implementation",
    "governed_implementation",
    "nontrivial_governed_work",
    "nontrivial_implementation",
    "strategic_judge_routed",
    "strategic_route_required",
)
_GOVERNED_NONTRIVIAL_TEXT_MARKERS = {
    "governed_nontrivial",
    "governed_nontrivial_work",
    "governed_nontrivial_implementation",
    "judge_routed",
    "judgment_routed",
    "parent_judge_routed",
    "strategic_judge_routed",
    "parent_route_required",
}
_GRAPH_TRACE_ID_KEYS = {
    "trace_id",
    "trace_ids",
    "graph_trace_id",
    "graph_trace_ids",
    "graph_query_trace_id",
    "graph_query_trace_ids",
}
_GRAPH_TRACE_CONTAINER_KEYS = (
    "graph_trace_evidence",
    "graph_query_trace_evidence",
    "mf_subagent_graph_trace",
    "graph_evidence",
)
_SERVICE_DISPATCH_CONTAINER_KEYS = (
    "service_dispatch_evidence",
    "observer_subagent_service_dispatch",
    "observer_subagent_service_dispatch_evidence",
    "subagent_service_dispatch",
    "spawn_agent_evidence",
)
_SAFE_ROUTE_IDENTITY_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
    "selected_project",
    "selected_backlog_id",
)
_DEFAULT_RUNTIME_CONTRACT_EVIDENCE = (
    "branch_runtime_registration",
    "observer_subagent_service_dispatch",
    "mf_subagent_startup",
    "graph_trace_evidence",
)
_BRANCH_RUNTIME_CONTAINER_KEYS = (
    "branch_runtime_evidence",
    "branch_runtime_context",
    "parallel_branch_runtime_context",
    "branch_context",
)
_BRANCH_RUNTIME_SOURCE_REF_KEYS = {
    "allocation_source_ref",
    "api_ref",
    "api_route",
    "endpoint",
    "function_ref",
    "registration_ref",
    "registration_source",
    "source_ref",
}
_BRANCH_RUNTIME_SOURCE_MARKERS = (
    "/parallel-branches/allocate",
    "parallel-branches/allocate",
    "upsert_branch_context",
)
_SERVICE_DISPATCH_COMMAND_REF_KEYS = {
    "dispatch_command_ref",
    "command_ref",
    "replay_command_ref",
    "spawn_command_ref",
    "command_id",
}
_SERVICE_DISPATCH_MONITOR_REF_KEYS = {
    "monitor_ref",
    "monitor_command_ref",
    "session_monitor_ref",
    "status_ref",
    "log_ref",
}
_SERVICE_DISPATCH_BOUNDARY_KEYS = {
    "documented_host_adapter_boundary",
    "host_adapter_boundary",
    "host_adapter_boundary_ref",
}
_IMPLEMENTATION_ACTIONS = {
    "apply_patch",
    "apply_patch_within_target_files",
    "edit_file",
    "edit_files",
    "implementation_exec",
    "implementation_file_edit",
    "mutate_files",
    "run_implementation_command",
    "write_file",
    "write_files",
}
_OBSERVER_JUDGER_ROLES = {
    "observer",
    "judger",
    "reviewer",
    "independent_reviewer",
    "observer_independent_reviewer",
}
_WORKER_ROLES = {
    "implementation_worker",
    "mf_sub",
    "mf_subagent",
    "subagent",
    "worker",
}
_HIGH_RISK_ROUTE_PRIORITIES = {"P0", "P1"}
_PARALLEL_ROUTE_TOPOLOGIES = {
    "mf_parallel",
    "mf_parallel_v1",
    "mf_parallel.v1",
    "observer_led_parallel_lanes",
    "parallel",
    "parallel_lanes",
}
_HIGH_RISK_ROUTE_PATH_MARKERS = (
    "agent/governance/",
    "agent/mcp/",
    "frontend/dashboard/",
    "shared-volume/",
    "docs/governance/",
    "skills/aming-claw/",
)
_ROUTE_PROVIDER_STATUS_CONTAINER_KEYS = (
    "provider_runtime_status",
    "mcp_runtime_status",
    "runtime_status",
    "route_provider_runtime_status",
    "route_context_runtime_status",
    "route_precheck_runtime_status",
    "route_provider_status",
    "provider_status",
    "route_context_status",
)
_ROUTE_PROVIDER_HASH_PAIRS = (
    ("loaded_source_hash", "current_source_hash"),
    ("loaded_provider_source_hash", "current_provider_source_hash"),
    ("loaded_route_source_hash", "current_route_source_hash"),
)
_ROUTE_TOKEN_WAIVER_TYPES = {
    "manual_fix",
    "manual-fix",
    "manual_fix_route_gate",
    "observer_manual_fix",
    "same_worktree",
    "same-worktree",
}
_ROUTE_TOKEN_REQUIRED_FIELDS = (
    "route_context_hash",
    "prompt_contract_id",
    "caller_role",
    "allowed_action",
    "expires_at",
    "evidence_refs",
)


class MfSubagentContractError(ValueError):
    """Raised when an MF subagent payload violates the worker contract."""


def _string(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise MfSubagentContractError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MfSubagentContractError(f"{field_name} must be a list of strings")
        if item:
            result.append(item)
    return result


def _mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MfSubagentContractError(f"{field_name} must be a mapping")
    return dict(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _explicit_false(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "n", "off"}
    return False


def _normalize_worktree_path(path: str) -> str:
    token = _string(path)
    if not token:
        return ""
    return str(Path(token).expanduser().resolve())


def _nested_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_route_identity(route_identity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(route_identity or {})
    safe = {
        key: _string(source.get(key))
        for key in _SAFE_ROUTE_IDENTITY_FIELDS
        if _string(source.get(key))
    }
    safe["raw_private_context_exposed"] = False
    return safe


def canonical_contract_hash(value: Any) -> str:
    """Return a stable SHA-256 hash for compact sorted JSON contract material."""

    try:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        body = json.dumps(repr(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _string_list_from_mapping(source: Mapping[str, Any], *keys: str) -> list[str]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            result = [str(item).strip() for item in value if str(item or "").strip()]
            if result:
                return result
    return []


def _first_present_mapping(source: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def verification_route_policy_from_contract(
    contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Separate deterministic gates from tests, fixtures, Docker, AI, and impact actions."""

    source = dict(contract or {})
    policy = _first_present_mapping(
        source,
        "verification_route_policy",
        "verification_route",
        "test_flow_route",
        "test_route",
        "route_verification_policy",
    )
    precheck = _first_present_mapping(source, "precheck", "route_precheck", "precheck_evidence")
    real_ai = _first_present_mapping(
        policy,
        "real_ai_provider_calls",
        "real_ai",
        "provider_calls",
    )
    explicit_ai_authorized = bool(
        real_ai.get("authorized")
        or real_ai.get("allowed")
        or policy.get("real_ai_authorized")
        or precheck.get("real_ai_authorized")
        or precheck.get("provider_calls_authorized")
    )
    auth_refs = _string_list_from_mapping(
        real_ai,
        "authorization_refs",
        "authorized_by",
        "precheck_refs",
    )
    auth_refs.extend(
        item
        for item in _string_list_from_mapping(
            precheck,
            "authorization_refs",
            "precheck_refs",
            "evidence_refs",
        )
        if item not in auth_refs
    )

    deterministic = _string_list_from_mapping(
        policy,
        "deterministic_gates",
        "deterministic_gate_ids",
    ) or ["route_identity", "contract_hash", "fence_token", "dirty_scope"]
    local_tests = _string_list_from_mapping(policy, "local_tests", "test_commands")
    fixtures = _first_present_mapping(policy, "fixtures", "fixture_policy")
    docker = _first_present_mapping(policy, "docker", "docker_policy")
    impact = _first_present_mapping(
        policy,
        "post_verification_impact_actions",
        "impact_actions",
    )

    return {
        "schema_version": VERIFICATION_ROUTE_POLICY_SCHEMA_VERSION,
        "source": "contract_or_route_precheck",
        "deterministic_gates": deterministic,
        "local_tests": {
            "commands": local_tests,
            "allowed": True,
        },
        "fixtures": {
            "allowed": bool(fixtures.get("allowed", True)),
            "required": bool(fixtures.get("required", False)),
            "ids": _string_list_from_mapping(fixtures, "ids", "fixtures"),
        },
        "docker": {
            "allowed": bool(docker.get("allowed", False)),
            "requires_explicit_route": bool(docker.get("requires_explicit_route", True)),
            "services": _string_list_from_mapping(docker, "services", "compose_services"),
        },
        "real_ai_provider_calls": {
            "allowed": explicit_ai_authorized,
            "authorized": explicit_ai_authorized,
            "blocked_by_default": not explicit_ai_authorized,
            "authorization_refs": auth_refs,
            "providers": _string_list_from_mapping(real_ai, "providers", "provider_ids"),
            "reason": (
                "explicit_route_or_precheck_authorized"
                if explicit_ai_authorized
                else "blocked_without_explicit_route_or_precheck"
            ),
        },
        "post_verification_impact_actions": {
            "allowed": bool(impact.get("allowed", False)),
            "actions": _string_list_from_mapping(impact, "actions", "allowed_actions"),
            "requires_observer": bool(impact.get("requires_observer", True)),
        },
        "policy_separation": {
            "deterministic_gates_are_not_local_tests": True,
            "local_tests_are_not_provider_calls": True,
            "post_verification_impact_actions_are_not_verification": True,
        },
    }


def build_observer_owned_agent_task_contract(
    context: BranchTaskRuntimeContext,
    *,
    requester: str = "",
    observer_owner: str = "",
    executor_lane: str = MF_SUB_ROLE,
    verifier_lane: str = "qa",
    route_identity: Mapping[str, Any] | None = None,
    contract_version: str = "mf_parallel.v1",
    contract_revision_id: str = "",
    previous_revision_hash: str = "",
    actor_role: str = "",
    actor_session_id: str = "",
    timestamp: str = "",
    read_receipt_hash: str = "",
    gate_receipt_hash: str = "",
    allowed_actions: Sequence[str] | None = None,
    blocked_actions: Sequence[str] | None = None,
    required_evidence: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    target_fences: Sequence[str] | None = None,
    lease_deadline: str = "",
    lifecycle_state: str = "",
    visible_injection_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the public, role-scoped Contract handoff object for a worker lane."""

    route = _safe_route_identity(route_identity)
    timestamp_text = _string(timestamp) or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    evidence = [
        item
        for item in (required_evidence or _DEFAULT_RUNTIME_CONTRACT_EVIDENCE)
        if _string(item)
    ]
    core = {
        "schema_version": AGENT_TASK_CONTRACT_SCHEMA_VERSION,
        "source_of_truth": "Contract/Revision/Event",
        "contract_version": _string(contract_version) or "mf_parallel.v1",
        "contract_revision_id": _string(contract_revision_id),
        "project_id": context.project_id,
        "governance_project_id": context.governance_project_id or context.project_id,
        "target_project_id": context.target_project_id or context.project_id,
        "target_project_root": context.target_project_root,
        "backlog_id": context.backlog_id,
        "task_id": context.task_id,
        "parent_task_id": _parent_task_id_for_contract_view(context),
        "requester": _string(requester) or "backlog_route",
        "observer_owner": _string(observer_owner) or OBSERVER_COORDINATOR_ROLE,
        "executor_lane": _string(executor_lane) or MF_SUB_ROLE,
        "verifier_lane": _string(verifier_lane) or "qa",
        "route_identity": route,
        "visible_injection_manifest": dict(visible_injection_manifest or {}),
        "allowed_actions": list(allowed_actions or MF_SUB_ALLOWED_CAPABILITIES),
        "blocked_actions": list(blocked_actions or MF_SUB_FORBIDDEN_ACTIONS),
        "required_evidence": evidence,
        "target_files": [item for item in (target_files or []) if _string(item)],
        "target_fences": [item for item in (target_fences or []) if _string(item)]
        or [context.fence_token],
        "lease": {
            "lease_id": context.lease_id,
            "deadline": _string(lease_deadline) or context.lease_expires_at,
        },
        "lifecycle_state": _string(lifecycle_state) or context.status,
        "step_receipt_contract": {
            "required_fields": [
                "canonical_visible_contract_text_hash",
                "previous_revision_hash",
                "actor_role",
                "actor_session_id",
                "timestamp",
                "read_receipt_hash",
                "gate_receipt_hash",
            ],
            "previous_revision_hash": _string(previous_revision_hash),
            "actor_role": _string(actor_role),
            "actor_session_id": _string(actor_session_id),
            "timestamp": timestamp_text,
            "read_receipt_hash": _string(read_receipt_hash),
            "gate_receipt_hash": _string(gate_receipt_hash),
        },
    }
    core["canonical_visible_contract_text_hash"] = canonical_contract_hash(core)
    return core


def _parent_task_id_for_contract_view(context: BranchTaskRuntimeContext) -> str:
    return (
        context.root_task_id
        or context.chain_id
        or context.stage_task_id
        or context.task_id
    )


def mf_subagent_runtime_context_id(context: BranchTaskRuntimeContext) -> str:
    return runtime_context_id_for_branch_context(context)


def build_mf_subagent_runtime_contract_view(
    context: BranchTaskRuntimeContext,
    *,
    role: str = MF_SUB_ROLE,
    contract_version: str = "mf_parallel.v1",
    contract_revision_id: str = "",
    latest_revision_id: str = "",
    known_revision_id: str = "",
    poll_after_sec: int = 15,
    latest_revision: Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
    required_evidence: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a queryable, role-scoped runtime/contract view for a worker lane."""

    normalized_role = _string(role).lower().replace("-", "_") or MF_SUB_ROLE
    is_worker = normalized_role == MF_SUB_ROLE
    parent_task_id = (
        context.root_task_id
        or context.chain_id
        or context.stage_task_id
        or context.task_id
    )
    runtime_context_id = mf_subagent_runtime_context_id(context)
    latest_revision_text = _string(latest_revision_id or contract_revision_id)
    known_revision_text = _string(known_revision_id)
    contract_changed = bool(
        latest_revision_text and known_revision_text != latest_revision_text
    )
    try:
        poll_after = int(poll_after_sec)
    except (TypeError, ValueError):
        poll_after = 15
    poll_after = max(1, poll_after)
    evidence_ids = tuple(
        _string(item)
        for item in (required_evidence or _DEFAULT_RUNTIME_CONTRACT_EVIDENCE)
        if _string(item)
    )
    latest_revision_map = dict(latest_revision or {})
    latest_revision_payload = (
        dict(latest_revision_map.get("payload"))
        if isinstance(latest_revision_map.get("payload"), Mapping)
        else {}
    )
    latest_revision_receipt = (
        dict(latest_revision_payload.get("revision_receipt"))
        if isinstance(latest_revision_payload.get("revision_receipt"), Mapping)
        else {}
    )
    route_identity_safe = _safe_route_identity(route_identity)
    agent_task_contract = build_observer_owned_agent_task_contract(
        context,
        requester=_string(latest_revision_payload.get("requester")),
        observer_owner=_string(latest_revision_payload.get("observer_owner")),
        executor_lane=_string(latest_revision_payload.get("executor_lane")) or MF_SUB_ROLE,
        verifier_lane=_string(latest_revision_payload.get("verifier_lane")) or "qa",
        route_identity=route_identity_safe,
        contract_version=_string(contract_version) or "mf_parallel.v1",
        contract_revision_id=latest_revision_text,
        previous_revision_hash=_string(
            latest_revision_receipt.get("previous_revision_hash")
        ),
        actor_role=_string(latest_revision_receipt.get("actor_role")),
        actor_session_id=_string(latest_revision_receipt.get("actor_session_id")),
        timestamp=_string(latest_revision_receipt.get("timestamp")),
        read_receipt_hash=_string(latest_revision_receipt.get("read_receipt_hash")),
        gate_receipt_hash=_string(latest_revision_receipt.get("gate_receipt_hash")),
        allowed_actions=_string_list_from_mapping(
            latest_revision_payload,
            "allowed_actions",
        )
        or MF_SUB_ALLOWED_CAPABILITIES,
        blocked_actions=_string_list_from_mapping(
            latest_revision_payload,
            "blocked_actions",
        )
        or MF_SUB_FORBIDDEN_ACTIONS,
        required_evidence=evidence_ids,
        target_files=_string_list_from_mapping(
            latest_revision_payload,
            "target_files",
            "owned_files",
        ),
        target_fences=[context.fence_token],
        lease_deadline=context.lease_expires_at,
        lifecycle_state=context.status,
        visible_injection_manifest=(
            latest_revision_payload.get("visible_injection_manifest")
            if isinstance(latest_revision_payload.get("visible_injection_manifest"), Mapping)
            else {}
        ),
    )
    projection_watermark = (
        latest_revision_text
        or context.checkpoint_id
        or context.updated_at
        or context.head_commit
    )
    projection_status = "current"
    if contract_changed:
        projection_status = "stale"
    elif not latest_revision_text:
        projection_status = "no_revision"

    runtime_context = {
        "runtime_context_id": runtime_context_id,
        "project_id": context.project_id,
        "governance_project_id": context.governance_project_id or context.project_id,
        "target_project_id": context.target_project_id or context.project_id,
        "target_project_root": context.target_project_root,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": context.backlog_id,
        "worker_role": MF_SUB_ROLE,
        "worker_id": context.worker_id,
        "worker_slot_id": context.worker_slot_id or context.worker_id,
        "actual_host_worker_id": context.actual_host_worker_id,
        "agent_id": context.agent_id,
        "allocation_owner": context.allocation_owner or context.agent_id,
        "observer_allocation_owner": context.allocation_owner or context.agent_id,
        "host_startup_id": context.host_startup_id,
        "host_session_id": context.host_session_id,
        "attempt": context.attempt,
        "branch_ref": context.branch_ref,
        "ref_name": context.ref_name,
        "worktree_id": context.worktree_id,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "head_commit": context.head_commit,
        "target_head_commit": context.target_head_commit,
        "snapshot_id": context.snapshot_id,
        "projection_id": context.projection_id,
        "merge_queue_id": context.merge_queue_id,
        "merge_preview_id": context.merge_preview_id,
        "fence_token": context.fence_token,
        "status": context.status,
        "checkpoint_id": context.checkpoint_id,
        "replay_source": context.replay_source,
        "depends_on": list(context.depends_on),
        "lease": {
            "lease_id": context.lease_id,
            "lease_expires_at": context.lease_expires_at,
            "heartbeat_required": True,
        },
        "timestamps": {
            "created_at": context.created_at,
            "updated_at": context.updated_at,
        },
    }

    contract = {
        "source_of_truth": "Contract/Revision/Event",
        "contract_version": _string(contract_version) or "mf_parallel.v1",
        "contract_revision_id": latest_revision_text,
        "agent_task_contract_schema_version": AGENT_TASK_CONTRACT_SCHEMA_VERSION,
        "backend_contract": BACKEND_CONTRACT,
        "dispatch_default": DISPATCH_DEFAULT,
        "worktree_policy": WORKTREE_POLICY_MODE,
        "allowed_capabilities": list(MF_SUB_ALLOWED_CAPABILITIES),
        "forbidden_actions": list(MF_SUB_FORBIDDEN_ACTIONS),
        "required_output": list(MF_SUB_REQUIRED_OUTPUT),
        "required_evidence": list(evidence_ids),
        "graph_query": {
            "query_source": "mf_subagent",
            "schema_version": GRAPH_TRACE_SCHEMA_VERSION,
            "governance_project_id": context.governance_project_id or context.project_id,
            "target_project_id": context.target_project_id or context.project_id,
            "target_project_root": context.target_project_root,
            "required_context_fields": [
                "task_id",
                "parent_task_id",
                "worker_role",
                "fence_token",
                "governance_project_id",
                "target_project_id",
            ],
            "trace_ids_required_in_timeline": True,
        },
        "read_receipt_ordering": {
            "schema_version": "mf_subagent_read_receipt_ordering.v1",
            "timeline_event_kind": "mf_subagent_read_receipt",
            "required_before": [
                "graph_query",
                "startup",
                "implementation",
                "verification",
                "close_ready",
            ],
            "close_sensitive": True,
            "post_hoc_receipt_satisfies_gate": False,
        },
        "service_routes": {
            "runtime_contract": (
                "/api/graph-governance/{project_id}/parallel-branches/"
                "{task_id}/runtime-contract"
            ),
            "runtime_contract_by_context": (
                "/api/graph-governance/{project_id}/parallel-branches/"
                "runtime-contexts/{runtime_context_id}/runtime-contract"
            ),
            "append_contract_revision": (
                "/api/graph-governance/{project_id}/parallel-branches/"
                "{task_id}/runtime-contract/revisions"
            ),
            "graph_query": "/api/graph-governance/{project_id}/query",
            "checkpoint": "/api/graph-governance/{project_id}/parallel-branches/checkpoint",
            "finish_gate": (
                "/api/graph-governance/{project_id}/parallel-branches/finish-gate"
            ),
        },
        "contract_change_policy": {
            "source_of_truth": "contract_service",
            "single_handoff_source": "Contract/Revision/Event",
            "observer_mutation": "append_contract_revision_only",
            "worker_poll_required": True,
            "worker_receives_runtime_context_id": True,
            "raw_prompt_as_runtime_source": False,
            "append_only_revisions": True,
        },
        "protected_timeline_append": {
            "schema_version": "mf_subagent_protected_timeline_append.v1",
            "protected_action": "task_timeline_append",
            "protected_event_kinds": [
                "implementation",
                "verification",
                "close_ready",
                "checkpoint",
                "review_ready",
            ],
            "route_token": {
                "preferred": True,
                "route_token_ref": route_identity_safe.get("route_token_ref", ""),
                "required_fields": list(_ROUTE_TOKEN_REQUIRED_FIELDS),
                "allowed_action": "task_timeline_append",
                "route_identity": route_identity_safe,
            },
            "task_scoped_route_waiver": {
                "usable_when": (
                    "route token is unavailable and matching bounded dispatch, startup, "
                    "and independent verification timeline refs exist"
                ),
                "must_be_accepted": True,
                "allowed_action": "task_timeline_append",
                "required_fields": [
                    "route_waiver.accepted",
                    "route_waiver.waiver_type",
                    "route_waiver.reason",
                    "route_waiver.route_context_hash",
                    "route_waiver.prompt_contract_id",
                    "route_waiver.caller_role",
                    "route_waiver.scope.project_id",
                    "route_waiver.scope.backlog_id",
                    "route_waiver.scope.task_id",
                    "route_waiver.timeline_evidence",
                    "route_waiver.allowed_action",
                ],
                "scope": {
                    "project_id": context.governance_project_id or context.project_id,
                    "backlog_id": context.backlog_id,
                    "task_id": context.task_id,
                    "runtime_context_id": runtime_context_id,
                    "fence_token": context.fence_token,
                },
                "accepted_waiver_template": {
                    "accepted": True,
                    "waiver_type": "manual_fix",
                    "manual_fix": True,
                    "allowed_action": "task_timeline_append",
                    "caller_role": MF_SUB_ROLE,
                    "reason": "Task-scoped protected append after bounded worker evidence.",
                    "route_context_hash": route_identity_safe.get("route_context_hash", ""),
                    "prompt_contract_id": route_identity_safe.get("prompt_contract_id", ""),
                    "prompt_contract_hash": route_identity_safe.get("prompt_contract_hash", ""),
                    "scope": {
                        "project_id": context.governance_project_id or context.project_id,
                        "backlog_id": context.backlog_id,
                        "task_id": context.task_id,
                    },
                    "timeline_evidence_required": True,
                },
            },
        },
    }

    view: dict[str, Any] = {
        "schema_version": RUNTIME_CONTRACT_VIEW_SCHEMA_VERSION,
        "runtime_context_id": runtime_context_id,
        "latest_revision_id": latest_revision_text,
        "known_revision_id": known_revision_text,
        "contract_changed": contract_changed,
        "must_ack_revision": contract_changed,
        "poll_after_sec": poll_after,
        "role_scope": "worker" if is_worker else "operator",
        "runtime_context": runtime_context,
        "contract": contract,
        "agent_task_contract": agent_task_contract,
        "route_identity": route_identity_safe,
        "route_id": route_identity_safe.get("route_id", ""),
        "route_context_hash": route_identity_safe.get("route_context_hash", ""),
        "prompt_contract_id": route_identity_safe.get("prompt_contract_id", ""),
        "prompt_contract_hash": route_identity_safe.get("prompt_contract_hash", ""),
        "visible_injection_manifest_hash": route_identity_safe.get(
            "visible_injection_manifest_hash",
            "",
        ),
        "target_files": list(agent_task_contract.get("target_files") or []),
        "owned_files": list(agent_task_contract.get("target_files") or []),
        "verification_route_policy": verification_route_policy_from_contract(
            {
                **latest_revision_payload,
                "route_identity": route_identity_safe,
            }
        ),
        "contract_projection": {
            "schema_version": AGENT_TASK_CONTRACT_PROJECTION_SCHEMA_VERSION,
            "source_of_truth": "Contract/Revision/Event",
            "projected_surfaces": [
                "observer_command_queue",
                "task_timeline",
                "backlog_runtime_state",
                "dashboard_cards",
                "branch_runtime",
            ],
            "contract_derived_status": context.status,
            "projection_watermark": projection_watermark,
            "status": projection_status,
            "stale": projection_status == "stale",
            "divergent": False,
            "contract_hash": agent_task_contract["canonical_visible_contract_text_hash"],
        },
        "privacy_boundary": {
            "raw_private_context_exposed": False,
            "redacted_context_sources": [
                "raw_private_route_body",
                "observer_only_context",
                "hidden_context",
                "unmanifested_prompt_text",
            ],
        },
        "worker_runtime_query": {
            "runtime_context_id": runtime_context_id,
            "contract_version": contract["contract_version"],
            "contract_revision_id": contract["contract_revision_id"],
            "known_revision_id": known_revision_text,
            "poll_after_sec": poll_after,
            "task_id": context.task_id,
            "fence_token_required": True,
            "query_source": "contract_service",
        },
    }
    if latest_revision:
        view["latest_revision"] = dict(latest_revision)
    if not is_worker:
        view["observer_controls"] = {
            "can_append_contract_revision": True,
            "can_update_dispatch_intent": False,
            "can_mark_blocker": True,
            "must_not_directly_implement_worker_code": True,
        }
    return view


def build_mf_subagent_runtime_context_projection(
    context: BranchTaskRuntimeContext,
    *,
    latest_revision: BranchRuntimeContractRevision | Mapping[str, Any] | None = None,
    route_identity: Mapping[str, Any] | None = None,
    route_gate: Mapping[str, Any] | None = None,
    timeline_refs: Mapping[str, Any] | None = None,
    graph_trace_refs: Mapping[str, Any] | Sequence[str] | None = None,
    startup_gate: Mapping[str, Any] | None = None,
    finish_gate: Mapping[str, Any] | None = None,
    close_evidence: Mapping[str, Any] | None = None,
    target_files: Sequence[str] | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    required_evidence: Sequence[str] | None = None,
    role: str = MF_SUB_ROLE,
    fence_token: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    """Function contract for API/MCP/CLI Runtime Context Service adapters.

    Worker C can call this wrapper and expose the returned dict without
    reassembling gate fields or role filters in server.py, mcp_server.py, or
    cli.py. Source-owned raw records must stay with their owning services.
    """

    return build_runtime_context_projection(
        context,
        contract_revision=latest_revision,
        route_identity=route_identity,
        route_gate=route_gate,
        timeline_refs=timeline_refs,
        graph_trace_refs=graph_trace_refs,
        startup_gate=startup_gate,
        finish_gate=finish_gate,
        close_evidence=close_evidence,
        target_files=target_files,
        acceptance_criteria=acceptance_criteria,
        required_evidence=required_evidence,
        role=role,
        fence_token=fence_token or context.fence_token,
        generated_at=generated_at,
    ).to_dict()


def build_mf_subagent_worker_runtime_context_view(
    context: BranchTaskRuntimeContext,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return only runtime_context.worker_view.v1 for an mf_sub worker."""

    projection = build_mf_subagent_runtime_context_projection(context, **kwargs)
    return dict(projection["views"]["worker_view"])


def validate_mf_subagent_worker_runtime_context_view(
    view: Mapping[str, Any],
    *,
    context: BranchTaskRuntimeContext | None = None,
    expected_task_id: str = "",
    expected_fence_token: str = "",
) -> dict[str, Any]:
    """Validate a role-filtered worker view before handing it to mf_sub code."""

    source = _mapping(view, field_name="runtime_context_worker_view")
    if source.get("schema_version") != RUNTIME_CONTEXT_WORKER_VIEW_SCHEMA_VERSION:
        raise MfSubagentContractError(
            "runtime context worker view schema_version mismatch"
        )
    if _string(source.get("role")) != MF_SUB_ROLE:
        raise MfSubagentContractError("runtime context worker view requires role=mf_sub")
    task = _nested_mapping(source, "task")
    privacy = _nested_mapping(source, "privacy_boundary")
    role_policy = _nested_mapping(source, "role_filter_policy")
    if _bool(privacy.get("raw_private_context_exposed")):
        raise MfSubagentContractError(
            "runtime context worker view exposes raw private context"
        )
    if _bool(privacy.get("other_worker_contexts_exposed")):
        raise MfSubagentContractError(
            "runtime context worker view exposes other worker contexts"
        )
    if _bool(role_policy.get("raw_private_context_exposed")):
        raise MfSubagentContractError(
            "runtime context role filter exposes raw private context"
        )
    if _bool(role_policy.get("other_worker_contexts_exposed")):
        raise MfSubagentContractError(
            "runtime context role filter exposes other worker contexts"
        )

    expected_task = _string(expected_task_id)
    expected_fence = _string(expected_fence_token)
    if context is not None:
        expected_task = expected_task or context.task_id
        expected_fence = expected_fence or context.fence_token
    if expected_task and _string(task.get("task_id")) != expected_task:
        raise MfSubagentContractError("runtime context worker view task_id mismatch")
    if expected_fence and _string(task.get("fence_token")) != expected_fence:
        raise MfSubagentContractError("runtime context worker view fence_token mismatch")

    gate_inputs = _nested_mapping(source, "gate_inputs")
    if gate_inputs.get("schema_version") != RUNTIME_CONTEXT_GATE_INPUTS_SCHEMA_VERSION:
        raise MfSubagentContractError(
            "runtime context worker view gate_inputs schema_version mismatch"
        )
    return {
        "schema_version": "mf_subagent_runtime_context_worker_view_validation.v1",
        "ok": True,
        "runtime_context_id": _string(source.get("runtime_context_id")),
        "task_id": _string(task.get("task_id")),
        "fence_token_matches": True,
        "raw_private_context_exposed": False,
        "other_worker_contexts_exposed": False,
        "gate_status": _string(gate_inputs.get("status")),
        "missing": list(gate_inputs.get("missing") or []),
    }


def _deep_field_values(
    value: Any,
    keys: set[str],
    *,
    depth: int = 0,
    max_depth: int = 5,
) -> list[Any]:
    if depth > max_depth:
        return []
    values: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in keys:
                values.append(child)
            values.extend(
                _deep_field_values(child, keys, depth=depth + 1, max_depth=max_depth)
            )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            values.extend(
                _deep_field_values(child, keys, depth=depth + 1, max_depth=max_depth)
            )
    return values


def _trace_id_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        result: list[str] = []
        for key in _GRAPH_TRACE_ID_KEYS:
            if key in value:
                result.extend(_trace_id_strings(value.get(key)))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result: list[str] = []
        for item in value:
            result.extend(_trace_id_strings(item))
        return result
    return []


def _first_deep_string(value: Any, keys: set[str]) -> str:
    for item in _deep_field_values(value, keys):
        if isinstance(item, Mapping):
            for key in ("id", "ref", "name", "value", "description"):
                token = _string(item.get(key))
                if token:
                    return token
            continue
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in item:
                token = _string(child)
                if token:
                    return token
            continue
        token = _string(item)
        if token:
            return token
    return ""


def _dispatch_string(
    payload: Mapping[str, Any],
    *,
    names: Sequence[str],
    nested_keys: Sequence[tuple[str, Sequence[str]]] = (),
) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, Mapping):
            for nested_name in (
                "branch_ref",
                "ref_name",
                "name",
                "path",
                "worktree_path",
            ):
                token = _string(value.get(nested_name))
                if token:
                    return token
        token = _string(value)
        if token:
            return token
    for parent_key, child_names in nested_keys:
        nested = _nested_mapping(payload, parent_key)
        for child_name in child_names:
            token = _string(nested.get(child_name))
            if token:
                return token
    return ""


def _route_prompt_contract_id(payload: Mapping[str, Any]) -> str:
    prompt_contract = _nested_mapping(payload, "prompt_contract")
    route_prompt_contract = _nested_mapping(payload, "route_prompt_contract")
    route_context = _nested_mapping(payload, "route_context")
    route_prompt_bundle = _nested_mapping(payload, "route_prompt_bundle")
    bundle = _nested_mapping(payload, "bundle")
    route_prompt_bundle_prompt_contract = _nested_mapping(
        route_prompt_bundle, "prompt_contract"
    )
    bundle_prompt_contract = _nested_mapping(bundle, "prompt_contract")
    return _string(
        payload.get("prompt_contract_id")
        or prompt_contract.get("prompt_contract_id")
        or prompt_contract.get("id")
        or route_prompt_contract.get("prompt_contract_id")
        or route_prompt_contract.get("id")
        or route_context.get("prompt_contract_id")
        or route_prompt_bundle.get("prompt_contract_id")
        or route_prompt_bundle_prompt_contract.get("prompt_contract_id")
        or route_prompt_bundle_prompt_contract.get("id")
        or bundle.get("prompt_contract_id")
        or bundle_prompt_contract.get("prompt_contract_id")
        or bundle_prompt_contract.get("id")
    )


def _route_context_hash(payload: Mapping[str, Any]) -> str:
    prompt_contract = _nested_mapping(payload, "prompt_contract")
    route_prompt_contract = _nested_mapping(payload, "route_prompt_contract")
    route_context = _nested_mapping(payload, "route_context")
    route_prompt_bundle = _nested_mapping(payload, "route_prompt_bundle")
    bundle = _nested_mapping(payload, "bundle")
    return _string(
        payload.get("route_context_hash")
        or route_context.get("route_context_hash")
        or prompt_contract.get("route_context_hash")
        or route_prompt_contract.get("route_context_hash")
        or route_prompt_bundle.get("route_context_hash")
        or bundle.get("route_context_hash")
    )


def _route_prompt_contract_hash(payload: Mapping[str, Any]) -> str:
    prompt_contract = _nested_mapping(payload, "prompt_contract")
    route_prompt_contract = _nested_mapping(payload, "route_prompt_contract")
    route_context = _nested_mapping(payload, "route_context")
    route_prompt_bundle = _nested_mapping(payload, "route_prompt_bundle")
    bundle = _nested_mapping(payload, "bundle")
    return _string(
        payload.get("prompt_contract_hash")
        or prompt_contract.get("prompt_contract_hash")
        or route_context.get("prompt_contract_hash")
        or route_prompt_contract.get("prompt_contract_hash")
        or route_prompt_bundle.get("prompt_contract_hash")
        or bundle.get("prompt_contract_hash")
    )


def _alert_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MfSubagentContractError("route_alerts must be a list of alerts")
    codes: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            code = _string(item)
        elif isinstance(item, Mapping):
            code = _string(item.get("code"))
        else:
            continue
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def _route_alert_codes(payload: Mapping[str, Any]) -> list[str]:
    candidates = [
        payload.get("route_alerts"),
        payload.get("alerts"),
    ]
    for key in ("route_context", "route_prompt_bundle", "bundle"):
        nested = _nested_mapping(payload, key)
        candidates.extend([nested.get("route_alerts"), nested.get("alerts")])

    codes: list[str] = []
    seen: set[str] = set()
    for alerts in candidates:
        for code in _alert_codes(alerts):
            if code and code not in seen:
                codes.append(code)
                seen.add(code)
    return codes


def _normalized_action(value: Any) -> str:
    return _string(value).lower().replace("-", "_").replace(".", "_")


def _route_action_name(payload: Mapping[str, Any], action: str = "") -> str:
    candidates: list[Any] = [
        action,
        payload.get("action"),
        payload.get("requested_action"),
        payload.get("tool_name"),
    ]
    for container in _route_identity_containers(payload):
        candidates.extend([
            container.get("action"),
            container.get("requested_action"),
            container.get("tool_name"),
        ])
    for candidate in candidates:
        token = _normalized_action(candidate)
        if token:
            return token
    return ""


def _route_caller_role(payload: Mapping[str, Any]) -> str:
    candidates: list[Any] = [
        payload.get("caller_role"),
        payload.get("role"),
        payload.get("actor_role"),
    ]
    for container in _route_identity_containers(payload):
        candidates.extend([
            container.get("caller_role"),
            container.get("role"),
            container.get("actor_role"),
        ])
    for candidate in candidates:
        token = _string(candidate).lower()
        if token:
            return token
    return ""


def _route_machine_containers(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [payload]
    for key in (
        "route",
        "route_context",
        "route_prompt_bundle",
        "bundle",
        "prompt_contract",
        "route_prompt_contract",
        "worker_prompt_contract",
        "verification_policy",
        "hashes",
    ):
        nested = _nested_mapping(payload, key)
        if nested:
            containers.append(nested)
            for child_key in (
                "route",
                "prompt_contract",
                "route_prompt_contract",
                "worker_prompt_contract",
                "verification_policy",
                "hashes",
            ):
                child = _nested_mapping(nested, child_key)
                if child:
                    containers.append(child)
    return containers


def _route_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        for key in (
            "id",
            "name",
            "role",
            "action",
            "allowed_action",
            "requirement_id",
            "evidence_id",
        ):
            token = _string(value.get(key))
            if token:
                return [token]
        return ["<mapping>"] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result: list[str] = []
        for item in value:
            result.extend(_route_text_values(item))
        return result
    token = _string(value)
    return [token] if token else []


def _route_collect_texts(
    payload: Mapping[str, Any],
    *field_names: str,
) -> list[str]:
    values: list[str] = []
    for container in _route_machine_containers(payload):
        for field_name in field_names:
            values.extend(_route_text_values(container.get(field_name)))
    return _dedupe_strings(values)


def _route_alert_mappings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    alerts: list[Mapping[str, Any]] = []
    for container in _route_machine_containers(payload):
        for key in ("route_alerts", "alerts"):
            value = container.get(key)
            if isinstance(value, Mapping):
                alerts.append(value)
            elif isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                alerts.extend(item for item in value if isinstance(item, Mapping))
    return alerts


def _route_blocked_actions(payload: Mapping[str, Any]) -> list[str]:
    values = _route_collect_texts(payload, "blocked_actions", "blocked_action")
    for alert in _route_alert_mappings(payload):
        values.extend(_route_text_values(alert.get("blocked_actions")))
    return _dedupe_strings(values)


def _route_hard_blocked_actions(
    payload: Mapping[str, Any],
    *,
    caller_role: str,
) -> list[str]:
    values = _route_collect_texts(payload, "blocked_actions", "blocked_action")
    for alert in _route_alert_mappings(payload):
        if not _route_alert_applies_to_role(alert, caller_role=caller_role):
            continue
        alert_code = _string(alert.get("code"))
        if (
            caller_role in _OBSERVER_JUDGER_ROLES
            and alert_code in ROUTE_DIRECT_IMPLEMENTATION_BLOCK_ALERTS
        ):
            continue
        values.extend(_route_text_values(alert.get("blocked_actions")))
    return _dedupe_strings(values)


def _route_alert_applies_to_role(alert: Mapping[str, Any], *, caller_role: str) -> bool:
    applies_to = {
        _normalized_action(item)
        for item in _route_text_values(alert.get("applies_to"))
    }
    if not applies_to:
        return True
    role = _normalized_action(caller_role)
    if not role:
        return True
    aliases = {role}
    if role in {"implementation_worker", "implementation", "worker", "mf_sub"}:
        aliases.update({"implementation_worker", "implementation", "worker", "mf_sub"})
    if role in {"qa", "reviewer", "independent_reviewer", "verification"}:
        aliases.update({"qa", "reviewer", "independent_reviewer", "verification"})
    if role in {"observer", "judger", "judge"}:
        aliases.update({"observer", "judger", "judge"})
    return bool(aliases.intersection(applies_to))


def _route_explicit_allowed_actions(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(payload, "allowed_actions", "allowed_action")


def _route_required_lanes(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(payload, "required_lanes", "required_lane")


def _route_required_evidence(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(
        payload,
        "required_evidence",
        "required_evidence_ids",
        "evidence_required",
        "evidence_requirements",
        "contract_evidence",
    )


def _lineage_text_list(value: Any) -> list[str]:
    return _dedupe_strings(
        value for value in _route_text_values(value) if value != "<mapping>"
    )


def _lineage_first_text(*values: Any) -> str:
    for value in values:
        texts = _lineage_text_list(value)
        if texts:
            return texts[0]
    return ""


def _parent_lineage_scope_value(
    packet: Mapping[str, Any],
    *,
    field_names: Sequence[str],
    scope_field_names: Sequence[str],
) -> str:
    scope = _nested_mapping(packet, "selected_scope")
    if not scope:
        scope = _nested_mapping(packet, "scope")
    if not scope:
        scope = _nested_mapping(packet, "route_scope")
    return _lineage_first_text(
        *(packet.get(field_name) for field_name in field_names),
        *(scope.get(field_name) for field_name in scope_field_names),
    )


def _parent_route_required(payload: Mapping[str, Any]) -> bool:
    containers: list[Mapping[str, Any]] = [payload]
    containers.extend(
        _nested_mapping(payload, key)
        for key in _PARENT_ROUTE_LINEAGE_CONTAINER_KEYS
        if _nested_mapping(payload, key)
    )
    for container in containers:
        for key in _PARENT_ROUTE_REQUIRED_KEYS:
            if _bool(container.get(key)):
                return True
    return False


def _has_parent_route_markers(packet: Mapping[str, Any]) -> bool:
    return any(_string(packet.get(key)) for key in _PARENT_ROUTE_LINEAGE_MARKER_KEYS)


def _parent_route_lineage_source(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    for key in _PARENT_ROUTE_LINEAGE_KEYS:
        if key in payload:
            packet = _mapping(payload.get(key), field_name=key)
            return packet, bool(packet)

    for container_key in _PARENT_ROUTE_LINEAGE_CONTAINER_KEYS:
        container = _nested_mapping(payload, container_key)
        if not container:
            continue
        for key in _PARENT_ROUTE_LINEAGE_KEYS:
            if key in container:
                packet = _mapping(container.get(key), field_name=key)
                return packet, bool(packet)
        if container_key in {"route_lineage", "lineage"} and _has_parent_route_markers(
            container
        ):
            return dict(container), True

    if _has_parent_route_markers(payload):
        return dict(payload), True
    return {}, False


def _normalize_parent_route_lineage(
    payload: Mapping[str, Any] | None,
    *,
    required: bool = False,
    project_id: str = "",
    backlog_id: str = "",
) -> dict[str, Any]:
    packet = dict(payload) if isinstance(payload, Mapping) else {}
    route = _nested_mapping(packet, "route")
    prompt_contract = _nested_mapping(packet, "prompt_contract")
    hashes = _nested_mapping(packet, "hashes")
    route_machine_context = _nested_mapping(packet, "route_machine_context")
    if not route_machine_context:
        route_machine_context = _nested_mapping(packet, "machine_context")
    selected_project = _parent_lineage_scope_value(
        packet,
        field_names=(
            "selected_project",
            "selected_project_id",
            "selected_project_slug",
            "parent_selected_project",
            "parent_selected_project_id",
        ),
        scope_field_names=(
            "project_id",
            "project",
            "selected_project",
            "selected_project_id",
        ),
    )
    selected_backlog_id = _parent_lineage_scope_value(
        packet,
        field_names=(
            "selected_backlog_id",
            "selected_backlog",
            "parent_selected_backlog_id",
            "backlog_id",
            "root_backlog_id",
        ),
        scope_field_names=(
            "backlog_id",
            "selected_backlog_id",
            "root_backlog_id",
        ),
    )
    if not selected_backlog_id:
        selected_backlog_id = _lineage_first_text(
            packet.get("selected_backlog_ids"),
            packet.get("root_backlog_ids"),
            _nested_mapping(packet, "scope").get("backlog_ids"),
            _nested_mapping(packet, "selected_scope").get("backlog_ids"),
        )

    normalized = {
        "schema_version": PARENT_ROUTE_LINEAGE_SCHEMA_VERSION,
        "route_id": _lineage_first_text(
            packet.get("route_id"),
            packet.get("parent_route_id"),
            packet.get("judge_route_id"),
            packet.get("judgment_route_id"),
            route.get("route_id"),
            route.get("id"),
        ),
        "route_context_hash": _lineage_first_text(
            packet.get("parent_route_context_hash"),
            packet.get("route_context_hash"),
            packet.get("context_hash"),
            route.get("route_context_hash"),
        ),
        "prompt_contract_id": _lineage_first_text(
            packet.get("parent_prompt_contract_id"),
            packet.get("prompt_contract_id"),
            prompt_contract.get("prompt_contract_id"),
            prompt_contract.get("id"),
        ),
        "visible_injection_manifest_hash": _lineage_first_text(
            packet.get("parent_visible_injection_manifest_hash"),
            packet.get("visible_injection_manifest_hash"),
            hashes.get("visible_injection_manifest_hash"),
            route_machine_context.get("visible_injection_manifest_hash"),
        ),
        "selected_project": selected_project,
        "selected_backlog_id": selected_backlog_id,
        "allowed_actions": _lineage_text_list(
            packet.get("allowed_actions") or route_machine_context.get("allowed_actions")
        ),
        "blocked_actions": _lineage_text_list(
            packet.get("blocked_actions") or route_machine_context.get("blocked_actions")
        ),
        "required_lanes": _lineage_text_list(
            packet.get("required_lanes") or route_machine_context.get("required_lanes")
        ),
        "required_evidence": _lineage_text_list(
            packet.get("required_evidence")
            or packet.get("required_evidence_ids")
            or packet.get("evidence_required")
            or packet.get("evidence_requirements")
            or route_machine_context.get("required_evidence")
            or route_machine_context.get("required_evidence_ids")
            or route_machine_context.get("evidence_required")
        ),
    }

    missing = [
        field
        for field in _PARENT_ROUTE_LINEAGE_REQUIRED_FIELDS
        if not normalized[field]
    ]
    if missing and required:
        raise MfSubagentContractError(
            "parent_route_lineage missing required fields: " + ", ".join(missing)
        )
    if missing and not required:
        return {}

    expected_project = _string(project_id)
    if expected_project and normalized["selected_project"] != expected_project:
        raise MfSubagentContractError(
            "parent_route_lineage selected_project does not match dispatch project"
        )
    expected_backlog = _string(backlog_id)
    if expected_backlog and normalized["selected_backlog_id"] != expected_backlog:
        raise MfSubagentContractError(
            "parent_route_lineage selected_backlog_id does not match dispatch backlog"
        )
    return normalized


def _parent_route_lineage_from_payload(
    payload: Mapping[str, Any],
    *,
    project_id: str = "",
    backlog_id: str = "",
) -> dict[str, Any]:
    source, present = _parent_route_lineage_source(payload)
    required = _parent_route_required(payload) or present
    if required and not source:
        raise MfSubagentContractError("parent_route_lineage is required")
    return _normalize_parent_route_lineage(
        source,
        required=required,
        project_id=project_id,
        backlog_id=backlog_id,
    )


def _governed_nontrivial_graph_required(
    payload: Mapping[str, Any],
    *,
    parent_route_lineage: Mapping[str, Any] | None = None,
) -> bool:
    if parent_route_lineage:
        return True
    if _parent_route_required(payload):
        return True
    if any(
        _normalized_action(value) in _PARALLEL_ROUTE_TOPOLOGIES
        for value in _route_topology_values(payload)
    ):
        return True
    for container in _route_machine_containers(payload):
        if any(_bool(container.get(key)) for key in _GOVERNED_NONTRIVIAL_KEYS):
            return True
    marker_values = _route_collect_texts(
        payload,
        "work_class",
        "route_class",
        "route_kind",
        "route_type",
        "selected_topology",
        "recommended_topology",
        "topology",
        "classification",
    )
    return any(
        _normalized_action(value) in _GOVERNED_NONTRIVIAL_TEXT_MARKERS
        for value in marker_values
    )


def _graph_trace_source(
    payload: Mapping[str, Any],
    *,
    allow_payload_fallback: bool = True,
) -> dict[str, Any]:
    for key in _GRAPH_TRACE_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    if not allow_payload_fallback:
        return {}
    return dict(payload)


def _normalize_graph_trace_evidence(
    payload: Mapping[str, Any],
    *,
    required: bool,
    task_id: str = "",
    parent_task_id: str = "",
    worker_role: str = "",
    fence_token: str = "",
) -> dict[str, Any]:
    source = _graph_trace_source(payload, allow_payload_fallback=not required)
    trace_ids = _dedupe_strings(
        trace_id
        for item in _deep_field_values(source, _GRAPH_TRACE_ID_KEYS)
        for trace_id in _trace_id_strings(item)
    )
    query_source = _first_deep_string(source, {"query_source"})
    if query_source and query_source != "mf_subagent":
        raise MfSubagentContractError(
            "graph trace evidence must use query_source=mf_subagent"
        )
    if required and not query_source:
        raise MfSubagentContractError(
            "graph trace evidence requires explicit query_source=mf_subagent"
        )

    embedded_task_id = _first_deep_string(source, {"task_id"})
    embedded_parent_task_id = _first_deep_string(source, {"parent_task_id"})
    embedded_worker_role = _first_deep_string(source, {"worker_role", "role"})
    embedded_fence_token = _first_deep_string(source, {"fence_token"})

    if required and embedded_worker_role and embedded_worker_role != MF_SUB_ROLE:
        raise MfSubagentContractError(
            "graph trace evidence must use worker_role=mf_sub"
        )

    evidence_task_id = embedded_task_id if required else embedded_task_id or task_id
    evidence_parent_task_id = (
        embedded_parent_task_id
        if required
        else embedded_parent_task_id or parent_task_id
    )
    evidence_worker_role = (
        embedded_worker_role
        if required
        else embedded_worker_role or worker_role or MF_SUB_ROLE
    )
    evidence_fence_token = (
        embedded_fence_token if required else embedded_fence_token or fence_token
    )

    expected = {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_role": MF_SUB_ROLE if required else worker_role or MF_SUB_ROLE,
        "fence_token": fence_token,
    }
    observed = {
        "task_id": evidence_task_id,
        "parent_task_id": evidence_parent_task_id,
        "worker_role": evidence_worker_role,
        "fence_token": evidence_fence_token,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if expected_value and observed[field] and observed[field] != expected_value
    ]
    if mismatches:
        raise MfSubagentContractError(
            "graph trace evidence identity mismatch: " + ", ".join(sorted(mismatches))
        )

    missing_context = [
        field
        for field, value in observed.items()
        if field in {"task_id", "parent_task_id", "worker_role", "fence_token"}
        and not value
    ]
    if required and not trace_ids:
        raise MfSubagentContractError(
            "governed mf_sub dispatch/finish requires graph trace evidence"
        )
    if required and missing_context:
        raise MfSubagentContractError(
            "graph trace evidence missing required context: "
            + ", ".join(sorted(missing_context))
        )

    return {
        "schema_version": GRAPH_TRACE_SCHEMA_VERSION,
        "required": required,
        "present": bool(trace_ids),
        "query_source": query_source,
        "trace_ids": trace_ids,
        "task_id": observed["task_id"],
        "parent_task_id": observed["parent_task_id"],
        "worker_role": observed["worker_role"],
        "fence_token": observed["fence_token"],
    }


def _branch_runtime_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in _BRANCH_RUNTIME_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _branch_runtime_source_ref(source: Mapping[str, Any]) -> str:
    ref = _first_deep_string(source, _BRANCH_RUNTIME_SOURCE_REF_KEYS)
    if ref:
        return ref
    for value in _deep_field_values(source, {"source", "function", "handler", "route"}):
        token = _string(value)
        if any(marker in token for marker in _BRANCH_RUNTIME_SOURCE_MARKERS):
            return token
    return ""


def _source_ref_is_branch_runtime_registration(source_ref: str) -> bool:
    normalized = source_ref.strip()
    return any(marker in normalized for marker in _BRANCH_RUNTIME_SOURCE_MARKERS)


def _context_field(source: Mapping[str, Any], *keys: str) -> str:
    context = _nested_mapping(source, "context")
    if context:
        for key in keys:
            token = _string(context.get(key))
            if token:
                return token
    for key in keys:
        token = _string(source.get(key))
        if token:
            return token
    return _first_deep_string(source, set(keys))


def _normalize_branch_runtime_evidence(
    payload: Mapping[str, Any],
    *,
    required: bool,
    task_id: str = "",
    parent_task_id: str = "",
    fence_token: str = "",
    worktree_path: str = "",
    base_commit: str = "",
    target_head_commit: str = "",
    merge_queue_id: str = "",
) -> dict[str, Any]:
    source = _branch_runtime_source(payload)
    source_ref = _branch_runtime_source_ref(source)
    normalized = {
        "schema_version": BRANCH_RUNTIME_SCHEMA_VERSION,
        "required": required,
        "present": bool(source),
        "registered": False,
        "source_ref": source_ref,
        "runtime_context_id": _context_field(source, "runtime_context_id") if source else "",
        "governance_project_id": _context_field(source, "governance_project_id")
        if source
        else "",
        "target_project_id": _context_field(source, "target_project_id")
        if source
        else "",
        "target_project_root": _context_field(source, "target_project_root")
        if source
        else "",
        "allocation_owner": _context_field(
            source,
            "allocation_owner",
            "observer_allocation_owner",
        )
        if source
        else "",
        "observer_allocation_owner": _context_field(
            source,
            "observer_allocation_owner",
            "allocation_owner",
        )
        if source
        else "",
        "worker_slot_id": _context_field(source, "worker_slot_id", "worker_id")
        if source
        else "",
        "task_id": _context_field(source, "task_id") if source else "",
        "parent_task_id": (
            _context_field(source, "parent_task_id", "root_task_id", "chain_id")
            if source
            else ""
        ),
        "fence_token": _context_field(source, "fence_token") if source else "",
        "worktree_path": _context_field(source, "worktree_path", "worktree")
        if source
        else "",
        "base_commit": _context_field(source, "base_commit") if source else "",
        "target_head_commit": _context_field(source, "target_head_commit")
        if source
        else "",
        "merge_queue_id": _context_field(source, "merge_queue_id") if source else "",
    }
    if not required:
        normalized["registered"] = bool(
            normalized["present"]
            and _source_ref_is_branch_runtime_registration(source_ref)
        )
        return normalized

    if not source:
        raise MfSubagentContractError(
            "governed mf_sub dispatch requires branch runtime registration evidence"
        )
    if not _source_ref_is_branch_runtime_registration(source_ref):
        raise MfSubagentContractError(
            "branch runtime registration evidence must reference "
            "parallel-branches/allocate or upsert_branch_context"
        )

    required_fields = {
        "task_id": task_id,
        "fence_token": fence_token,
        "worktree_path": worktree_path,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
    }
    if parent_task_id:
        required_fields["parent_task_id"] = parent_task_id
    missing = [
        field
        for field, expected_value in required_fields.items()
        if expected_value and not normalized[field]
    ]
    if not normalized["runtime_context_id"]:
        missing.append("runtime_context_id")
    if missing:
        raise MfSubagentContractError(
            "branch runtime registration evidence missing required fields: "
            + ", ".join(sorted(missing))
        )

    mismatches = [
        field
        for field, expected_value in required_fields.items()
        if expected_value
        and normalized[field]
        and normalized[field] != expected_value
    ]
    if mismatches:
        raise MfSubagentContractError(
            "branch runtime registration evidence identity mismatch: "
            + ", ".join(sorted(mismatches))
        )
    normalized["registered"] = True
    return normalized


def _service_dispatch_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in _SERVICE_DISPATCH_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    if (
        _first_deep_string(payload, _SERVICE_DISPATCH_COMMAND_REF_KEYS)
        or _first_deep_string(payload, _SERVICE_DISPATCH_BOUNDARY_KEYS)
    ):
        return dict(payload)
    return {}


def _normalize_service_dispatch_evidence(
    payload: Mapping[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    source = _service_dispatch_source(payload)
    command_ref = _first_deep_string(source, _SERVICE_DISPATCH_COMMAND_REF_KEYS)
    monitor_ref = _first_deep_string(source, _SERVICE_DISPATCH_MONITOR_REF_KEYS)
    boundary_ref = _first_deep_string(source, _SERVICE_DISPATCH_BOUNDARY_KEYS)
    boundary_documentation_ref = _first_deep_string(
        source,
        {"boundary_documentation_ref", "documentation_ref", "adapter_contract_ref"},
    )
    documented_boundary = bool(boundary_ref) and (
        "documented_host_adapter_boundary" in source
        or _bool(source.get("documented"))
        or bool(boundary_documentation_ref)
    )
    replayable_refs = bool(command_ref and monitor_ref)
    present = replayable_refs or documented_boundary
    if required and not present:
        raise MfSubagentContractError(
            "observer_subagent_service_dispatch evidence must include replayable "
            "command/monitor refs or a documented host-adapter boundary"
        )
    return {
        "schema_version": SERVICE_DISPATCH_SCHEMA_VERSION,
        "required": required,
        "present": present,
        "replayable_refs_present": replayable_refs,
        "dispatch_command_ref": command_ref,
        "monitor_ref": monitor_ref,
        "documented_host_adapter_boundary": documented_boundary,
        "host_adapter_boundary": boundary_ref,
        "boundary_documentation_ref": boundary_documentation_ref,
    }


def _child_route_prompt_contract(
    *,
    route_context_hash: str = "",
    prompt_contract_id: str = "",
    prompt_contract_hash: str = "",
) -> dict[str, str]:
    return {
        "route_context_hash": _string(route_context_hash),
        "prompt_contract_id": _string(prompt_contract_id),
        "prompt_contract_hash": _string(prompt_contract_hash),
    }


def _child_route_prompt_contract_from_payload(
    payload: Mapping[str, Any],
) -> dict[str, str]:
    route_lineage = _nested_mapping(payload, "route_lineage")
    child_route = _nested_mapping(route_lineage, "child_route_prompt_contract")
    if not child_route:
        child_route = _nested_mapping(route_lineage, "child_route_identity")
    if not child_route:
        child_route = _nested_mapping(route_lineage, "route_prompt_contract")
    return _child_route_prompt_contract(
        route_context_hash=_route_context_hash(payload)
        or _string(child_route.get("route_context_hash")),
        prompt_contract_id=_route_prompt_contract_id(payload)
        or _string(child_route.get("prompt_contract_id") or child_route.get("id")),
        prompt_contract_hash=_route_prompt_contract_hash(payload)
        or _string(child_route.get("prompt_contract_hash")),
    )


def _mf_subagent_route_lineage(
    *,
    parent_route_lineage: Mapping[str, Any],
    child_route_prompt_contract: Mapping[str, Any],
) -> dict[str, Any]:
    parent = dict(parent_route_lineage) if parent_route_lineage else {}
    child = dict(child_route_prompt_contract)
    return {
        "schema_version": ROUTE_LINEAGE_SCHEMA_VERSION,
        "parent_route_lineage": parent,
        "child_route_prompt_contract": child,
        "parent_route_id": _string(parent.get("route_id")),
        "parent_route_context_hash": _string(parent.get("route_context_hash")),
        "parent_prompt_contract_id": _string(parent.get("prompt_contract_id")),
        "child_route_context_hash": _string(child.get("route_context_hash")),
        "child_prompt_contract_id": _string(child.get("prompt_contract_id")),
        "child_prompt_contract_hash": _string(child.get("prompt_contract_hash")),
    }


def _route_visible_injection_manifest_present(payload: Mapping[str, Any]) -> bool:
    for container in _route_machine_containers(payload):
        if _string(container.get("visible_injection_manifest_hash")):
            return True
        hashes = _nested_mapping(container, "hashes")
        if _string(hashes.get("visible_injection_manifest_hash")):
            return True
        manifest = container.get("visible_injection_manifest")
        if isinstance(manifest, Mapping) and bool(manifest):
            return True
    return False


def _route_priority(payload: Mapping[str, Any]) -> str:
    for container in _route_machine_containers(payload):
        for key in ("priority", "severity", "risk_priority"):
            token = _string(container.get(key)).upper()
            if token:
                return token
    return ""


def _route_topology_values(payload: Mapping[str, Any]) -> list[str]:
    values = _route_collect_texts(
        payload,
        "selected_topology",
        "recommended_topology",
        "topology",
    )
    return [_normalized_action(value) for value in values if _string(value)]


def _route_file_values(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(
        payload,
        "target_files",
        "test_files",
        "changed_files",
        "owned_files",
        "write_scope",
    )


def _route_cross_module_change(files: Sequence[str]) -> bool:
    normalized = [_string(path).replace("\\", "/") for path in files if _string(path)]
    buckets = {"/".join(path.split("/")[:2]) for path in normalized}
    return len(normalized) > 3 or len(buckets) > 1


def _route_action_high_risk_policy(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    priority = _route_priority(payload)
    topologies = _route_topology_values(payload)
    files = _route_file_values(payload)
    required_lanes = {
        _normalized_action(item) for item in _route_required_lanes(payload) if item
    }
    required_evidence = {
        _normalized_action(item) for item in _route_required_evidence(payload) if item
    }
    risk_class = _string(
        payload.get("risk_class")
        or payload.get("risk")
        or _nested_mapping(payload, "route_context").get("risk_class")
    ).lower()
    reason_codes: list[str] = []
    if priority in _HIGH_RISK_ROUTE_PRIORITIES:
        reason_codes.append(f"priority_{priority.lower()}")
    if any(topology in _PARALLEL_ROUTE_TOPOLOGIES for topology in topologies):
        reason_codes.append("parallel_route_topology")
    if required_lanes.intersection(
        {
            "bounded_implementation_worker",
            "independent_verification_lane",
            "observer_led_parallel_lanes",
            "parallel_lanes",
        }
    ):
        reason_codes.append("parallel_required_lanes")
    if required_evidence.intersection(
        {
            "bounded_implementation_worker_dispatch",
            "mf_subagent_dispatch",
            "mf_subagent_startup",
            "bounded_dispatch_evidence",
        }
    ):
        reason_codes.append("bounded_worker_evidence_required")
    if _route_cross_module_change(files):
        reason_codes.append("cross_module_change")
    if any(
        any(marker in _string(path).replace("\\", "/") for marker in _HIGH_RISK_ROUTE_PATH_MARKERS)
        for path in files
    ):
        reason_codes.append("high_risk_governance_surface")
    if risk_class in {"high", "critical", "p0", "p1", "high_risk"}:
        reason_codes.append("explicit_high_risk")
    return {
        "required": bool(reason_codes),
        "priority": priority,
        "topologies": topologies,
        "file_count": len(files),
        "reason_codes": _dedupe_strings(reason_codes),
    }


def _route_provider_unavailable_reason(payload: Mapping[str, Any]) -> str:
    bool_reason_fields = {
        "route_provider_unavailable": "route provider unavailable",
        "route_context_unavailable": "route context unavailable",
        "route_precheck_provider_unavailable": "route precheck provider unavailable",
        "provider_unavailable": "route provider unavailable",
        "mcp_unavailable": "route provider unavailable",
        "runtime_unavailable": "route runtime unavailable",
        "unavailable": "route provider unavailable",
        "transport_closed": "route provider transport closed",
        "transport_is_closed": "route provider transport closed",
        "closed": "route provider transport closed",
        "route_context_stale": "route context stale",
        "route_evidence_stale": "route evidence stale",
        "stale_route_evidence": "route evidence stale",
        "stale": "route runtime stale",
        "runtime_stale": "route runtime stale",
        "provider_stale": "route provider stale",
        "mcp_stale": "route MCP runtime stale",
    }
    status_fields = (
        "status",
        "state",
        "runtime_state",
        "transport_status",
        "connection_status",
        "availability",
        "route_provider_status",
        "provider_status",
        "mcp_status",
        "runtime_status",
        "route_context_status",
        "route_evidence_status",
        "route_action_precheck_status",
    )
    error_fields = (
        "route_provider_error",
        "provider_error",
        "route_context_error",
        "route_precheck_error",
        "error",
        "last_error",
        "message",
    )
    for prefix, container in _route_provider_status_containers(payload):
        for field_name, reason in bool_reason_fields.items():
            if _bool(container.get(field_name)):
                return f"{prefix}.{field_name}=True" if prefix else reason
        if _explicit_false(container.get("available")):
            return f"{prefix}.available=False" if prefix else "route provider unavailable"
        hash_mismatch = _route_provider_hash_mismatch(container)
        if hash_mismatch:
            return f"{prefix}.{hash_mismatch}" if prefix else hash_mismatch
        for field_name in status_fields:
            status = _normalized_action(container.get(field_name))
            if status in {
                "unavailable",
                "provider_unavailable",
                "route_provider_unavailable",
                "mcp_unavailable",
                "runtime_unavailable",
                "transport_closed",
                "transportclosed",
                "connection_closed",
                "closed",
                "stale",
                "runtime_stale",
                "provider_stale",
                "mcp_stale",
                "stale_evidence",
                "route_context_stale",
                "route_evidence_stale",
                "hash_mismatch",
                "source_hash_mismatch",
                "stale_hash_mismatch",
            }:
                name = f"{prefix}.{field_name}" if prefix else field_name
                return f"{name}={_string(container.get(field_name))}"
        for field_name in error_fields:
            raw_text = _string(container.get(field_name))
            text = raw_text.lower()
            normalized = _normalized_action(raw_text)
            if (
                "transport closed" in text
                or "transport_closed" in normalized
                or "transportclosed" in normalized
                or "closed transport" in text
                or "connection closed" in text
                or "provider unavailable" in text
                or "route context unavailable" in text
                or "stale route" in text
                or "route evidence stale" in text
                or "source hash mismatch" in text
            ):
                name = f"{prefix}.{field_name}" if prefix else field_name
                return f"{name}: {raw_text}" if prefix else raw_text
    return ""


def _route_provider_status_containers(
    payload: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    containers: list[tuple[str, Mapping[str, Any]]] = [
        ("", container) for container in _route_machine_containers(payload)
    ]
    seen = {id(container) for _, container in containers}
    for parent_prefix, parent in list(containers):
        for key in _ROUTE_PROVIDER_STATUS_CONTAINER_KEYS:
            child = parent.get(key)
            if not isinstance(child, Mapping) or id(child) in seen:
                continue
            prefix = f"{parent_prefix}.{key}" if parent_prefix else key
            containers.append((prefix, child))
            seen.add(id(child))
    return containers


def _route_provider_hash_mismatch(container: Mapping[str, Any]) -> str:
    for loaded_key, current_key in _ROUTE_PROVIDER_HASH_PAIRS:
        loaded = _string(container.get(loaded_key))
        current = _string(container.get(current_key))
        if loaded and current and loaded != current:
            return f"{loaded_key}/{current_key}_mismatch"
    return ""


def _route_identity_containers(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    route = _nested_mapping(payload, "route")
    if route:
        containers.append(route)
    for key in ("route_context", "route_prompt_bundle", "bundle"):
        nested = _nested_mapping(payload, key)
        if nested:
            containers.append(nested)
            nested_route = _nested_mapping(nested, "route")
            if nested_route:
                containers.append(nested_route)
    return containers


def _accepted_waiver_matches(
    waiver: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> bool:
    status = _string(waiver.get("status") or waiver.get("decision")).lower()
    accepted = _bool(waiver.get("accepted")) or status in {
        "accepted",
        "approved",
        "allow",
        "allowed",
        "waived",
    }
    if not accepted:
        return False
    waiver_prompt_hash = _string(waiver.get("prompt_contract_hash"))
    return (
        _string(waiver.get("route_context_hash")) == route_context_hash
        and _string(waiver.get("prompt_contract_id")) == prompt_contract_id
        and (
            not waiver_prompt_hash
            or not prompt_contract_hash
            or waiver_prompt_hash == prompt_contract_hash
        )
    )


def _dispatch_evidence_matches(
    evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> bool:
    status = _string(evidence.get("status") or evidence.get("decision")).lower()
    if (
        _explicit_false(evidence.get("allowed"))
        or _explicit_false(evidence.get("ok"))
        or status in _FAIL_STATUSES
    ):
        return False
    allowed = (
        _bool(evidence.get("allowed"))
        or _bool(evidence.get("ok"))
        or status in _PASS_STATUSES
        or status in {"allow", "allowed"}
        or _string(evidence.get("schema_version")) == DISPATCH_GATE_SCHEMA_VERSION
    )
    role = _string(
        evidence.get("role") or evidence.get("worker_role") or evidence.get("caller_role")
    ).lower()
    worker_role_ok = not role or role in _WORKER_ROLES or role == MF_SUB_ROLE
    evidence_prompt_hash = _string(evidence.get("prompt_contract_hash"))
    return (
        allowed
        and worker_role_ok
        and _string(evidence.get("route_context_hash")) == route_context_hash
        and _string(evidence.get("prompt_contract_id")) == prompt_contract_id
        and (
            not evidence_prompt_hash
            or not prompt_contract_hash
            or evidence_prompt_hash == prompt_contract_hash
        )
    )


def _fence_evidence_present(evidence: Mapping[str, Any]) -> bool:
    for key in (
        "fence_token",
        "worker_fence_token",
        "route_fence_token",
        "actual_fence_token",
        "reported_fence_token",
    ):
        if _string(evidence.get(key)):
            return True
    for key in (
        "fence_token_present",
        "actual_fence_token_present",
        "fence_token_matches",
    ):
        if _bool(evidence.get(key)):
            return True
    if _string(evidence.get("fence_token_hash")):
        return True
    fence = _nested_mapping(evidence, "fence")
    if _string(fence.get("token") or fence.get("fence_token") or fence.get("hash")):
        return True
    return False


def _actual_startup_identity_present(evidence: Mapping[str, Any]) -> bool:
    actual_runtime = _nested_mapping(evidence, "actual_runtime")
    actual_cwd = _string(evidence.get("actual_cwd") or actual_runtime.get("cwd"))
    actual_git_root = _string(
        evidence.get("actual_git_root") or actual_runtime.get("git_root")
    )
    worktree = _string(
        evidence.get("assigned_worktree")
        or evidence.get("worktree_path")
        or evidence.get("worktree")
        or actual_runtime.get("assigned_worktree")
        or actual_runtime.get("worktree_path")
        or actual_runtime.get("worktree")
    )
    if worktree:
        normalized_worktree = _normalize_worktree_path(worktree)
        if actual_cwd and _normalize_worktree_path(actual_cwd) != normalized_worktree:
            return False
        if actual_git_root and _normalize_worktree_path(actual_git_root) != normalized_worktree:
            return False
    branch = _string(
        evidence.get("branch")
        or evidence.get("branch_ref")
        or actual_runtime.get("branch")
        or actual_runtime.get("branch_ref")
    )
    head_commit = _string(
        evidence.get("head_commit")
        or evidence.get("branch_head")
        or actual_runtime.get("head_commit")
        or actual_runtime.get("branch_head")
    )
    return bool(
        (actual_cwd or actual_git_root)
        and branch
        and head_commit
        and _fence_evidence_present(evidence)
    )


def _startup_intent_only(evidence: Mapping[str, Any]) -> bool:
    schema_version = _string(evidence.get("schema_version"))
    kind = _string(
        evidence.get("gate_kind")
        or evidence.get("kind")
        or evidence.get("intent_kind")
    ).lower()
    return (
        schema_version == "mf_subagent_startup_intent.v1"
        or "startup_intent" in kind
        or _explicit_false(evidence.get("close_satisfying"))
    )


def _bounded_dispatch_evidence_present(evidence: Mapping[str, Any]) -> bool:
    if _explicit_false(evidence.get("bounded")):
        return False
    return (
        _bool(evidence.get("bounded"))
        or _string(evidence.get("schema_version")) == DISPATCH_GATE_SCHEMA_VERSION
        or bool(
            _string(evidence.get("worktree") or evidence.get("worktree_path"))
            and _string(evidence.get("fence_token"))
        )
    )


def _bounded_startup_evidence_present(evidence: Mapping[str, Any]) -> bool:
    if _explicit_false(evidence.get("bounded")) or _startup_intent_only(evidence):
        return False
    gate_kind = _string(evidence.get("gate_kind") or evidence.get("kind")).lower()
    schema_version = _string(evidence.get("schema_version"))
    bounded_signal = (
        _bool(evidence.get("bounded"))
        or schema_version == "mf_subagent_startup_gate.v1"
        or gate_kind == "mf_subagent.startup"
        or _bool(evidence.get("same_as_expected_worker"))
        or _bool(evidence.get("fence_token_matches"))
        or bool(
            _string(evidence.get("worktree") or evidence.get("worktree_path"))
            and _fence_evidence_present(evidence)
        )
    )
    return bounded_signal and _actual_startup_identity_present(evidence)


def _startup_evidence_matches(
    evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> bool:
    if _startup_intent_only(evidence) or not _actual_startup_identity_present(evidence):
        return False
    status = _string(evidence.get("status") or evidence.get("decision")).lower()
    if (
        _explicit_false(evidence.get("allowed"))
        or _explicit_false(evidence.get("ok"))
        or status in _FAIL_STATUSES
    ):
        return False
    gate_kind = _string(evidence.get("gate_kind") or evidence.get("kind")).lower()
    allowed = (
        _bool(evidence.get("allowed"))
        or _bool(evidence.get("ok"))
        or status in _PASS_STATUSES
        or status in {"allow", "allowed"}
        or gate_kind == "mf_subagent.startup"
        or _bool(evidence.get("started"))
        or _bool(evidence.get("startup_complete"))
        or _bool(evidence.get("same_as_expected_worker"))
    )
    role = _string(
        evidence.get("role") or evidence.get("worker_role") or evidence.get("caller_role")
    ).lower()
    worker_role_ok = not role or role in _WORKER_ROLES or role == MF_SUB_ROLE
    evidence_prompt_hash = _string(evidence.get("prompt_contract_hash"))
    return (
        allowed
        and worker_role_ok
        and _string(evidence.get("route_context_hash")) == route_context_hash
        and _string(evidence.get("prompt_contract_id")) == prompt_contract_id
        and (
            not evidence_prompt_hash
            or not prompt_contract_hash
            or evidence_prompt_hash == prompt_contract_hash
        )
    )


def _route_startup_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    direct = _mapping(
        payload.get("bounded_startup_evidence")
        or payload.get("startup_evidence")
        or payload.get("mf_subagent_startup_gate"),
        field_name="bounded_startup_evidence",
    )
    if direct:
        return direct
    evidence = _mapping(payload.get("evidence"), field_name="evidence")
    nested = _mapping(
        evidence.get("bounded_startup_evidence")
        or evidence.get("startup_evidence")
        or evidence.get("mf_subagent_startup_gate"),
        field_name="evidence.startup_evidence",
    )
    if nested:
        return nested
    for key in ("startup_timeline_event", "generated_startup_timeline_event"):
        event = _mapping(payload.get(key), field_name=key)
        if not event:
            continue
        if _string(event.get("event_kind")) != "mf_subagent_startup":
            continue
        event_payload = _nested_mapping(event, "payload")
        gate = _nested_mapping(event_payload, "mf_subagent_startup_gate")
        if gate:
            return gate
    return {}


def _bounded_worker_evidence_matches(
    dispatch_evidence: Mapping[str, Any],
    startup_evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> dict[str, Any]:
    dispatch_matches = _dispatch_evidence_matches(
        dispatch_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    startup_matches = _startup_evidence_matches(
        startup_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    dispatch_fence = _fence_evidence_present(dispatch_evidence)
    startup_fence = _fence_evidence_present(startup_evidence)
    dispatch_bounded = _bounded_dispatch_evidence_present(dispatch_evidence)
    startup_bounded = _bounded_startup_evidence_present(startup_evidence)
    dispatch_present = dispatch_matches and dispatch_fence and dispatch_bounded
    startup_present = startup_matches and startup_fence and startup_bounded
    return {
        "present": dispatch_present and startup_present,
        "dispatch_present": dispatch_present,
        "startup_present": startup_present,
        "dispatch_matches": dispatch_matches,
        "startup_matches": startup_matches,
        "fence_present": dispatch_fence and startup_fence,
        "bounded_present": dispatch_bounded and startup_bounded,
        "dispatch_fence_present": dispatch_fence,
        "startup_fence_present": startup_fence,
        "dispatch_bounded_present": dispatch_bounded,
        "startup_bounded_present": startup_bounded,
    }


def _first_mapping(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _version_workspace_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _first_mapping(
        payload,
        (
            "version_check",
            "workspace_gate",
            "workspace_evidence",
            "version_gate",
        ),
    )
    if not evidence:
        return {"present": False, "passed": False, "reason": "missing"}
    status = _string(evidence.get("status") or evidence.get("result")).lower()
    dirty = _bool(evidence.get("dirty"))
    dirty_files = _string_list(evidence.get("dirty_files"), field_name="dirty_files")
    passed_signal = (
        _bool(evidence.get("ok"))
        or _bool(evidence.get("passed"))
        or status in _PASS_STATUSES
    )
    passed = passed_signal and not dirty and not dirty_files
    reason = ""
    if dirty:
        reason = "dirty worktree"
    elif dirty_files:
        reason = "dirty files present"
    elif not passed_signal:
        reason = "not passed"
    return {
        "present": True,
        "passed": passed,
        "status": status or ("passed" if passed_signal else ""),
        "dirty": dirty,
        "dirty_file_count": len(dirty_files),
        "reason": reason,
    }


def _graph_current_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _first_mapping(
        payload,
        (
            "graph_status",
            "graph_gate",
            "current_graph",
            "graph_evidence",
        ),
    )
    if not evidence:
        return {"present": False, "passed": False, "reason": "missing"}
    current_state = _mapping(evidence.get("current_state"), field_name="current_state")
    raw_graph_stale = evidence.get("graph_stale")
    if isinstance(raw_graph_stale, Mapping):
        graph_stale = dict(raw_graph_stale)
    elif isinstance(current_state.get("graph_stale"), Mapping):
        graph_stale = dict(current_state["graph_stale"])
    else:
        graph_stale = {}
    stale_known = "is_stale" in graph_stale or isinstance(raw_graph_stale, bool)
    if "is_stale" in graph_stale:
        stale = _bool(graph_stale.get("is_stale"))
    elif isinstance(raw_graph_stale, bool):
        stale = raw_graph_stale
    else:
        stale = False
    status = _string(evidence.get("status") or evidence.get("result")).lower()
    passed_signal = (
        _bool(evidence.get("ok"))
        or _bool(evidence.get("passed"))
        or _bool(evidence.get("current"))
        or _bool(evidence.get("graph_current"))
        or status in _PASS_STATUSES
        or (stale_known and not stale)
    )
    passed = passed_signal and not stale
    reason = "graph stale" if stale else ("" if passed_signal else "not current")
    return {
        "present": True,
        "passed": passed,
        "status": status or ("passed" if passed_signal else ""),
        "graph_stale": stale,
        "reason": reason,
    }


def validate_route_action_gate(
    payload: Mapping[str, Any],
    *,
    action: str = "",
) -> dict[str, Any]:
    """Validate route-owned role/action policy before implementation mutation."""

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("route action gate payload must be a mapping")
    payload = dict(payload)
    action_name = _route_action_name(payload, action=action)
    caller_role = _route_caller_role(payload)
    route_context_hash = _route_context_hash(payload)
    prompt_contract_id = _route_prompt_contract_id(payload)
    prompt_contract_hash = _route_prompt_contract_hash(payload)
    route_alert_codes = _route_alert_codes(payload)
    implementation_action = action_name in _IMPLEMENTATION_ACTIONS
    high_risk_policy = _route_action_high_risk_policy(payload)
    provider_unavailable_reason = _route_provider_unavailable_reason(payload)
    direct_implementation_block_alerts = sorted(
        ROUTE_DIRECT_IMPLEMENTATION_BLOCK_ALERTS.intersection(route_alert_codes)
    )
    waiver = _mapping(
        payload.get("route_action_waiver")
        or payload.get("accepted_waiver")
        or payload.get("waiver"),
        field_name="route_action_waiver",
    )
    waiver_matches = _accepted_waiver_matches(
        waiver,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )

    if implementation_action and provider_unavailable_reason:
        raise MfSubagentContractError(
            "blocked_route_context_unavailable: "
            f"{provider_unavailable_reason}"
        )
    if implementation_action and (not route_context_hash or not prompt_contract_id):
        raise MfSubagentContractError(
            "implementation action requires route_context_hash and prompt_contract_id"
        )
    if (
        caller_role in _OBSERVER_JUDGER_ROLES
        and implementation_action
        and direct_implementation_block_alerts
    ):
        alert_code = ",".join(direct_implementation_block_alerts)
        raise MfSubagentContractError(
            f"{alert_code} blocks {caller_role or 'unknown'} direct implementation "
            f"action {action_name or 'unknown'}; route waiver, dispatch, or startup "
            "evidence cannot authorize observer/reviewer direct implementation"
        )
    if implementation_action and not _route_visible_injection_manifest_present(payload):
        raise MfSubagentContractError(
            "implementation action requires visible_injection_manifest_hash "
            "or visible_injection_manifest"
        )
    machine_context = {
        "visible_injection_manifest_present": _route_visible_injection_manifest_present(
            payload
        ),
        "allowed_actions": _route_explicit_allowed_actions(payload),
        "blocked_actions": _route_blocked_actions(payload),
        "required_lanes": _route_required_lanes(payload),
        "required_evidence": _route_required_evidence(payload),
    }
    if implementation_action and high_risk_policy["required"]:
        missing_machine_fields: list[str] = []
        if not caller_role:
            missing_machine_fields.append("caller_role")
        if not machine_context["visible_injection_manifest_present"]:
            missing_machine_fields.append("visible_injection_manifest")
        for field_name in (
            "allowed_actions",
            "blocked_actions",
            "required_lanes",
            "required_evidence",
        ):
            if not machine_context[field_name]:
                missing_machine_fields.append(field_name)
        if missing_machine_fields:
            raise MfSubagentContractError(
                "high-risk implementation action requires machine route "
                "context fields: " + ", ".join(missing_machine_fields)
            )
        if not _route_action_allowed(action_name, machine_context["allowed_actions"]):
            raise MfSubagentContractError(
                f"route allowed_actions do not allow implementation action {action_name}"
            )
    hard_blocked_actions = _route_hard_blocked_actions(
        payload,
        caller_role=caller_role,
    )
    if implementation_action and _route_action_allowed(action_name, hard_blocked_actions):
        raise MfSubagentContractError(
            "route blocked_actions explicitly block implementation action "
            f"{action_name}"
        )
    dispatch_evidence = _mapping(
        payload.get("bounded_dispatch_evidence")
        or payload.get("dispatch_evidence")
        or payload.get("mf_subagent_dispatch_gate"),
        field_name="bounded_dispatch_evidence",
    )
    startup_evidence = _route_startup_evidence(payload)
    bounded_worker_evidence = _bounded_worker_evidence_matches(
        dispatch_evidence,
        startup_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    dispatch_matches = bool(bounded_worker_evidence["dispatch_present"])
    startup_matches = bool(bounded_worker_evidence["startup_present"])
    bounded_worker_matches = bool(bounded_worker_evidence["present"])
    if (
        implementation_action
        and high_risk_policy["required"]
        and not bounded_worker_matches
    ):
        raise MfSubagentContractError(
            "high-risk implementation action requires matching bounded dispatch/startup "
            "evidence before mutation"
        )
    version_workspace_gate = _version_workspace_gate(payload)
    graph_current_gate = _graph_current_gate(payload)
    precondition_waiver_used = False
    if implementation_action:
        if not version_workspace_gate["passed"]:
            if not waiver_matches:
                raise MfSubagentContractError(
                    "implementation action requires clean version/workspace evidence"
                )
            precondition_waiver_used = True
        if not graph_current_gate["passed"]:
            if not waiver_matches:
                raise MfSubagentContractError(
                    "implementation action requires current graph evidence"
                )
            precondition_waiver_used = True

    return {
        "schema_version": ROUTE_ACTION_GATE_SCHEMA_VERSION,
        "allowed": True,
        "action": action_name,
        "caller_role": caller_role,
        "implementation_action": implementation_action,
        "route_alert_codes": route_alert_codes,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "machine_context_required": bool(high_risk_policy["required"]),
        "machine_context_policy": high_risk_policy,
        "route_machine_context": machine_context,
        "accepted_waiver_present": waiver_matches,
        "bounded_dispatch_evidence_present": dispatch_matches,
        "bounded_startup_evidence_present": startup_matches,
        "bounded_worker_evidence_present": bounded_worker_matches,
        "bounded_dispatch_evidence": bounded_worker_evidence,
        "version_workspace_gate": version_workspace_gate,
        "graph_current_gate": graph_current_gate,
        "precondition_waiver_used": precondition_waiver_used,
    }


def validate_route_token_mutation_gate(
    payload: Mapping[str, Any],
    *,
    action: str,
    project_id: str = "",
    backlog_id: str = "",
    task_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate route-token or explicit waiver evidence for protected mutations."""

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("route token gate payload must be a mapping")
    payload = dict(payload)
    action_name = _normalized_action(action)
    if not action_name:
        raise MfSubagentContractError("route token gate requires an action")

    request_scope = {
        "project_id": _string(project_id),
        "backlog_id": _string(backlog_id),
        "task_id": _string(task_id),
    }
    token = _route_token_payload(payload)
    if token:
        return _validate_route_token(
            token,
            action=action_name,
            request_scope=request_scope,
            now=now,
        )

    waiver = _route_token_waiver(payload)
    if waiver:
        return _validate_route_token_waiver(
            waiver,
            action=action_name,
            request_scope=request_scope,
        )

    raise MfSubagentContractError(
        f"route_token is required for protected governance action {action_name}; "
        "pass route_token with route_context_hash, prompt_contract_id, caller_role, "
        "allowed action, scope, expiry, and evidence_refs, or pass an explicit "
        "route_waiver with reason and timeline evidence"
    )


def route_token_required_failure_details(
    *,
    action: str,
    reason: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return machine-readable details for expected protected-route failures."""

    action_name = _normalized_action(action) or _string(action)
    details: dict[str, Any] = {
        "schema_version": ROUTE_TOKEN_REQUIRED_FAILURE_SCHEMA_VERSION,
        "protected_action": action_name,
        "route_token_required": True,
        "fault_domain": "caller_missing_route_evidence",
        "expected_behavior": True,
        "do_not_file_system_bug": True,
        "is_system_bug": False,
        "classification": "expected_protected_route_gate",
        "required_route_token_fields": [
            "route_context_hash",
            "prompt_contract_id",
            "caller_role",
            "allowed_action",
            "scope.project_id",
            "expires_at",
            "evidence_refs",
        ],
        "waiver_fields": [
            "route_waiver.waiver_type",
            "route_waiver.reason",
            "route_waiver.route_context_hash",
            "route_waiver.prompt_contract_id",
            "route_waiver.caller_role",
            "route_waiver.scope.project_id",
            "route_waiver.scope.backlog_id",
            "route_waiver.scope.task_id",
            "route_waiver.timeline_evidence",
            "route_waiver.allowed_action",
        ],
        "next_valid_actions": [
            "return_to_route_context_and_request_a_valid_route_token",
            "dispatch_or_start_the_bounded_mf_subagent_worker_and_record_route_context_consumption",
            "record_route_waiver_as_waiver_evidence_only_when_no_route_token_is_available",
            "retry_the_protected_action_only_after_matching_route_token_or_required_route_evidence_exists",
        ],
        "system_bug_preconditions": [
            "a_valid_unexpired_route_token_with_matching_action_scope_and_evidence_refs_was_supplied",
            "or_required_bounded_worker_route_context_consumption_evidence_exists_with_matching_route_identity",
            "and_the_protected_gate_still_rejected_or_stripped_the_structured_route_details",
        ],
    }
    if reason:
        details["reason"] = reason
    if isinstance(extra, Mapping):
        details.update(dict(extra))
    return details


def _route_token_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    token = payload.get("route_token")
    if isinstance(token, str):
        try:
            parsed = json.loads(token)
        except json.JSONDecodeError:
            parsed = {}
        token = parsed
    if isinstance(token, Mapping):
        return dict(token)
    return {}


def _route_token_waiver(payload: Mapping[str, Any]) -> dict[str, Any]:
    waiver = (
        payload.get("route_waiver")
        or payload.get("route_token_waiver")
        or payload.get("protected_route_waiver")
    )
    if isinstance(waiver, str):
        try:
            parsed = json.loads(waiver)
        except json.JSONDecodeError:
            parsed = {}
        waiver = parsed
    if isinstance(waiver, Mapping):
        return dict(waiver)
    return {}


def _validate_route_token(
    token: Mapping[str, Any],
    *,
    action: str,
    request_scope: Mapping[str, str],
    now: datetime | None,
) -> dict[str, Any]:
    route_context_hash = _string(token.get("route_context_hash"))
    prompt_contract_id = _string(token.get("prompt_contract_id"))
    prompt_contract_hash = _string(token.get("prompt_contract_hash"))
    caller_role = _string(token.get("caller_role") or token.get("role")).lower()
    expires_at = _string(token.get("expires_at") or token.get("expiry"))
    evidence_refs = _route_evidence_refs(token)
    allowed_actions = _route_allowed_actions(token)

    missing = []
    if not route_context_hash:
        missing.append("route_context_hash")
    if not prompt_contract_id:
        missing.append("prompt_contract_id")
    if not caller_role:
        missing.append("caller_role")
    if not allowed_actions:
        missing.append("allowed_action")
    if not expires_at:
        missing.append("expires_at")
    if not evidence_refs:
        missing.append("evidence_refs")
    if missing:
        raise MfSubagentContractError(
            "route_token missing required fields: " + ", ".join(missing)
        )

    if not _route_action_allowed(action, allowed_actions):
        raise MfSubagentContractError(
            f"route_token does not allow protected action {action}"
        )

    expires_dt = _parse_route_expiry(expires_at)
    now_dt = now or datetime.now(timezone.utc)
    if expires_dt <= now_dt:
        raise MfSubagentContractError("route_token expired")

    _validate_route_scope(token, request_scope=request_scope)
    return {
        "schema_version": ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION,
        "allowed": True,
        "status": "accepted",
        "action": action,
        "decision": "route_token",
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "caller_role": caller_role,
        "route_token_hash": _stable_hash(token),
        "expires_at": expires_at,
        "evidence_refs": evidence_refs,
        "scope": _route_scope_summary(token, request_scope),
        "required_fields": list(_ROUTE_TOKEN_REQUIRED_FIELDS),
    }


def _validate_route_token_waiver(
    waiver: Mapping[str, Any],
    *,
    action: str,
    request_scope: Mapping[str, str],
) -> dict[str, Any]:
    route_context_hash = _route_context_hash(waiver)
    prompt_contract_id = _route_prompt_contract_id(waiver)
    caller_role = _route_caller_role(waiver)
    missing_identity = []
    if not route_context_hash:
        missing_identity.append("route_context_hash")
    if not prompt_contract_id:
        missing_identity.append("prompt_contract_id")
    if not caller_role:
        missing_identity.append("caller_role")
    if missing_identity:
        raise MfSubagentContractError(
            "route_waiver missing required route identity fields: "
            + ", ".join(missing_identity)
        )

    status = _string(waiver.get("status") or waiver.get("decision")).lower()
    accepted = _bool(waiver.get("accepted")) or status in {
        "accepted",
        "approved",
        "allow",
        "allowed",
        "waived",
    }
    if not accepted:
        raise MfSubagentContractError("route_waiver must be explicitly accepted")

    waiver_type = _string(
        waiver.get("waiver_type")
        or waiver.get("type")
        or waiver.get("kind")
    ).lower()
    manual_fix = _bool(waiver.get("manual_fix") or waiver.get("manual_fix_allowed"))
    same_worktree = _bool(
        waiver.get("same_worktree_allowed") or waiver.get("same_worktree")
    )
    if waiver_type not in _ROUTE_TOKEN_WAIVER_TYPES and not (manual_fix or same_worktree):
        raise MfSubagentContractError(
            "route_waiver requires manual_fix or same_worktree waiver type"
        )

    reason = _string(waiver.get("reason") or waiver.get("operator_reason"))
    if len(reason) < 20:
        raise MfSubagentContractError(
            "route_waiver requires reason with at least 20 characters"
        )

    allowed_actions = _route_allowed_actions(waiver)
    if not allowed_actions or not _route_action_allowed(action, allowed_actions):
        raise MfSubagentContractError(
            f"route_waiver does not allow protected action {action}"
        )

    timeline_evidence = _timeline_evidence_refs(waiver)
    if not timeline_evidence:
        raise MfSubagentContractError(
            "route_waiver requires timeline evidence"
        )

    _validate_route_scope(waiver, request_scope=request_scope)
    return {
        "schema_version": ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION,
        "allowed": True,
        "status": "accepted",
        "action": action,
        "decision": "route_waiver",
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "caller_role": caller_role,
        "waiver_hash": _stable_hash(waiver),
        "waiver_type": waiver_type or ("manual_fix" if manual_fix else "same_worktree"),
        "reason": reason,
        "timeline_evidence": timeline_evidence,
        "scope": _route_scope_summary(waiver, request_scope),
    }


def _route_allowed_actions(value: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = [
        value.get("allowed_action"),
        value.get("action"),
        value.get("requested_action"),
    ]
    allowed_actions = value.get("allowed_actions")
    if isinstance(allowed_actions, Sequence) and not isinstance(
        allowed_actions, (str, bytes, bytearray)
    ):
        candidates.extend(allowed_actions)
    elif allowed_actions:
        candidates.append(allowed_actions)
    actions: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        action = _normalized_action(candidate)
        if action and action not in seen:
            actions.append(action)
            seen.add(action)
    return actions


def _route_action_allowed(action: str, allowed_actions: Sequence[str]) -> bool:
    allowed = {_normalized_action(item) for item in allowed_actions if _string(item)}
    return "*" in allowed or _normalized_action(action) in allowed


def _route_evidence_refs(token: Mapping[str, Any]) -> list[str]:
    refs = _string_list_forgiving(token.get("evidence_refs"))
    for key in ("evidence_ref", "trace_id", "timeline_event_id", "source_event_id"):
        ref = _string(token.get(key))
        if ref:
            refs.append(ref)
    return _dedupe_strings(refs)


def _timeline_evidence_refs(waiver: Mapping[str, Any]) -> list[str]:
    refs = _string_list_forgiving(waiver.get("timeline_evidence_refs"))
    for key in ("timeline_event_id", "event_id", "trace_id"):
        ref = _string(waiver.get(key))
        if ref:
            refs.append(ref)
    timeline_evidence = waiver.get("timeline_evidence")
    if isinstance(timeline_evidence, Mapping):
        for key in ("event_id", "id", "trace_id"):
            ref = _string(timeline_evidence.get(key))
            if ref:
                refs.append(ref)
    return _dedupe_strings(refs)


def _string_list_forgiving(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        token = _string(value)
        return [token] if token else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            ref = _string(
                item.get("id")
                or item.get("event_id")
                or item.get("trace_id")
                or item.get("ref")
            )
        else:
            ref = _string(item)
        if ref:
            refs.append(ref)
    return refs


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _string(value)
        if token and token not in seen:
            out.append(token)
            seen.add(token)
    return out


def _parse_route_expiry(value: str) -> datetime:
    raw = _string(value)
    if not raw:
        raise MfSubagentContractError("route_token expires_at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MfSubagentContractError("route_token expires_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _route_scope_value(value: Mapping[str, Any], *names: str) -> str:
    scope = _nested_mapping(value, "scope")
    for name in names:
        token = _string(value.get(name) or scope.get(name))
        if token:
            return token
    return ""


def _validate_route_scope(
    value: Mapping[str, Any],
    *,
    request_scope: Mapping[str, str],
) -> None:
    project_id = _string(request_scope.get("project_id"))
    if project_id:
        token_project_id = _route_scope_value(value, "project_id")
        if not token_project_id:
            raise MfSubagentContractError("route token scope requires project_id")
        if token_project_id != project_id:
            raise MfSubagentContractError(
                f"route token project scope {token_project_id!r} does not match {project_id!r}"
            )

    backlog_id = _string(request_scope.get("backlog_id"))
    if backlog_id:
        token_backlog_id = _route_scope_value(value, "backlog_id", "bug_id")
        if not token_backlog_id:
            raise MfSubagentContractError("route token scope requires backlog_id")
        if token_backlog_id != backlog_id:
            raise MfSubagentContractError(
                f"route token backlog scope {token_backlog_id!r} does not match {backlog_id!r}"
            )

    task_id = _string(request_scope.get("task_id"))
    if task_id:
        token_task_id = _route_scope_value(value, "task_id")
        if not token_task_id:
            raise MfSubagentContractError("route token scope requires task_id")
        if token_task_id != task_id:
            raise MfSubagentContractError(
                f"route token task scope {token_task_id!r} does not match {task_id!r}"
            )


def _route_scope_summary(
    value: Mapping[str, Any],
    request_scope: Mapping[str, str],
) -> dict[str, str]:
    return {
        "project_id": _route_scope_value(value, "project_id") or _string(request_scope.get("project_id")),
        "backlog_id": _route_scope_value(value, "backlog_id", "bug_id") or _string(request_scope.get("backlog_id")),
        "task_id": _route_scope_value(value, "task_id") or _string(request_scope.get("task_id")),
    }


def _stable_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dirty_scope_evidence(value: Any) -> dict[str, Any]:
    check = _mapping(value, field_name="dirty_scope_check")
    status = _string(check.get("status") or check.get("result")).lower()
    passed = _bool(check.get("passed")) or status in _PASS_STATUSES
    exact_match = _bool(
        check.get("dirty_scope_exact_match")
        or check.get("exact_match")
        or check.get("owned_scope_only")
    )
    evidence_fields = [
        "changed_files",
        "dirty_files",
        "owned_files",
        "checked_paths",
        "allowed_dirty_files",
    ]
    has_file_evidence = any(key in check for key in evidence_fields)
    if not passed or not has_file_evidence:
        raise MfSubagentContractError(
            "dirty_scope_check must include passing dirty-scope evidence"
        )
    return {
        "status": status or "passed",
        "passed": True,
        "dirty_scope_exact_match": exact_match,
        "changed_file_count": len(
            _string_list(check.get("changed_files"), field_name="changed_files")
        ),
        "dirty_file_count": len(
            _string_list(check.get("dirty_files"), field_name="dirty_files")
        ),
    }


def _override_reason(payload: Mapping[str, Any], override: Mapping[str, Any]) -> str:
    for key in (
        "same_worktree_reason",
        "operator_reason",
        "explicit_operator_reason",
        "reason",
    ):
        token = _string(payload.get(key))
        if token:
            return token
    for key in ("operator_reason", "explicit_operator_reason", "reason"):
        token = _string(override.get(key))
        if token:
            return token
    return ""


def _timeline_evidence_present(
    payload: Mapping[str, Any],
    override: Mapping[str, Any],
    *,
    require_before_mutation: bool = False,
) -> bool:
    if _bool(payload.get("timeline_event_recorded")) or _bool(
        override.get("timeline_event_recorded")
    ):
        return not require_before_mutation
    for key in (
        "timeline_evidence",
        "observer_timeline_event",
        "dispatch_timeline_evidence",
        "direct_mutation_timeline_evidence",
    ):
        value = payload.get(key) if key in payload else override.get(key)
        if isinstance(value, Mapping) and (
            _string(value.get("event_id"))
            or _string(value.get("event_type"))
            or _string(value.get("recorded_at"))
        ):
            if require_before_mutation and not (
                _bool(value.get("recorded_before_mutation"))
                or _bool(value.get("before_mutation"))
                or _string(value.get("phase")).lower()
                in {"pre_mutation", "before_mutation", "pre_implementation"}
            ):
                continue
            return True
    return False


def _direct_mutation_exception(payload: Mapping[str, Any]) -> dict[str, Any]:
    exception = _nested_mapping(payload, "observer_direct_mutation_exception")
    if not exception:
        exception = _nested_mapping(payload, "direct_mutation_exception")
    return exception


def validate_observer_direct_mutation_exception(
    payload: Mapping[str, Any],
    *,
    allowed_files: Sequence[str] | None = None,
    dirty_files: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate the narrow exception for observer-authored mutations.

    Governed nontrivial implementation belongs in bounded `mf_sub` or worker
    lanes. This validator is only for tiny deterministic same-observer edits
    and is separate from worker dispatch so the observer role boundary is
    machine-checkable.
    """

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError(
            "observer direct mutation exception payload must be a mapping"
        )
    payload = dict(payload)
    direct_mutation = _bool(
        payload.get("observer_direct_mutation")
        or payload.get("same_observer_direct_mutation")
        or payload.get("direct_mutation")
    )
    if not direct_mutation:
        raise MfSubagentContractError(
            "observer direct mutation exception requires observer_direct_mutation=true"
        )

    exception = _direct_mutation_exception(payload)
    role = _string(
        payload.get("observer_role")
        or payload.get("role")
        or payload.get("actor")
        or exception.get("observer_role")
        or exception.get("role")
        or exception.get("actor")
    ).lower()
    if role != OBSERVER_COORDINATOR_ROLE:
        raise MfSubagentContractError(
            "observer direct mutation exception requires observer role evidence"
        )

    tiny_deterministic = _bool(
        exception.get("tiny_deterministic")
        or exception.get("tiny_deterministic_scope")
        or payload.get("tiny_deterministic")
        or payload.get("tiny_deterministic_scope")
    )
    if not tiny_deterministic:
        raise MfSubagentContractError(
            "observer direct mutation exception requires tiny deterministic scope"
        )

    reason = _override_reason(payload, exception)
    if not reason:
        raise MfSubagentContractError(
            "observer direct mutation exception requires explicit reason"
        )

    exception_allowed_files = _string_list(
        exception.get("allowed_files") or payload.get("allowed_files"),
        field_name="allowed_files",
    )
    if not exception_allowed_files:
        raise MfSubagentContractError(
            "observer direct mutation exception requires allowed_files"
        )
    expected_allowed_files = set(
        _string_list(allowed_files, field_name="allowed_files")
    )
    if expected_allowed_files and not set(exception_allowed_files).issubset(
        expected_allowed_files
    ):
        raise MfSubagentContractError(
            "observer direct mutation exception allowed_files exceed owned scope"
        )

    dirty_scope_input = exception.get("dirty_scope_check") or payload.get(
        "dirty_scope_check"
    )
    if not dirty_scope_input:
        if dirty_files is None:
            raise MfSubagentContractError(
                "observer direct mutation exception requires dirty-scope evidence"
            )
        dirty_scope_input = {
            "status": "passed",
            "dirty_scope_exact_match": True,
            "dirty_files": list(dirty_files),
            "owned_files": exception_allowed_files,
        }
    dirty_scope = _dirty_scope_evidence(dirty_scope_input)
    dirty_scope_mapping = _mapping(
        dirty_scope_input,
        field_name="dirty_scope_check",
    )
    if not dirty_scope["dirty_scope_exact_match"]:
        raise MfSubagentContractError(
            "observer direct mutation exception requires dirty_scope_exact_match evidence"
        )
    scoped_dirty_files = set(
        _string_list(
            dirty_scope_mapping.get("dirty_files")
            or dirty_scope_mapping.get("changed_files")
            or dirty_files,
            field_name="dirty_files",
        )
    )
    if scoped_dirty_files and not scoped_dirty_files.issubset(
        set(exception_allowed_files)
    ):
        raise MfSubagentContractError(
            "observer direct mutation exception dirty files must match allowed_files"
        )

    if not _timeline_evidence_present(
        payload,
        exception,
        require_before_mutation=True,
    ):
        raise MfSubagentContractError(
            "observer direct mutation exception requires timeline evidence before mutation"
        )

    return {
        "schema_version": OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION,
        "role": OBSERVER_COORDINATOR_ROLE,
        "policy_default": OBSERVER_DIRECT_MUTATION_DEFAULT,
        "observer_direct_mutation": True,
        "allowed": True,
        "exception": {
            "used": True,
            "tiny_deterministic": True,
            "reason": reason,
            "allowed_files": exception_allowed_files,
            "timeline_evidence_recorded_before_mutation": True,
        },
        "dirty_scope_check": dirty_scope,
    }


def validate_mf_subagent_dispatch_gate(
    payload: Mapping[str, Any],
    *,
    target_worktree_path: str = "",
    main_worktree_path: str = "",
) -> dict[str, Any]:
    """Validate local MF subagent dispatch evidence before handoff.

    The gate is intentionally local and backend-neutral: observers and AI
    self-checks can run it before spawning a bounded `mf_sub` worker.
    """

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("MF subagent dispatch payload must be a mapping")
    payload = dict(payload)
    branch = _dispatch_string(
        payload,
        names=("branch", "branch_ref", "ref_name"),
        nested_keys=(("branch_context", ("branch_ref", "ref_name")),),
    )
    worktree = _dispatch_string(
        payload,
        names=("worktree", "worktree_path"),
        nested_keys=(("branch", ("worktree_path", "path")),),
    )
    base_commit = _dispatch_string(
        payload,
        names=("base_commit",),
        nested_keys=(("branch", ("base_commit",)),),
    )
    target_head_commit = _dispatch_string(
        payload,
        names=("target_head_commit",),
        nested_keys=(("branch", ("target_head_commit", "head_commit")),),
    )
    merge_queue_id = _dispatch_string(
        payload,
        names=("merge_queue_id",),
        nested_keys=(
            ("branch_context", ("merge_queue_id",)),
            ("graph_identity", ("merge_queue_id",)),
            ("branch", ("merge_queue_id",)),
        ),
    )
    fence_token = _dispatch_string(payload, names=("fence_token",))
    route_context_hash = _dispatch_string(
        payload,
        names=("route_context_hash",),
        nested_keys=(
            ("route_context", ("route_context_hash",)),
            ("prompt_contract", ("route_context_hash",)),
            ("route_prompt_contract", ("route_context_hash",)),
        ),
    )
    prompt_contract_id = _dispatch_string(
        payload,
        names=("prompt_contract_id",),
        nested_keys=(
            ("route_context", ("prompt_contract_id",)),
            ("prompt_contract", ("prompt_contract_id", "id")),
            ("route_prompt_contract", ("prompt_contract_id", "id")),
        ),
    )
    prompt_contract_hash = _dispatch_string(
        payload,
        names=("prompt_contract_hash",),
        nested_keys=(
            ("route_context", ("prompt_contract_hash",)),
            ("prompt_contract", ("prompt_contract_hash",)),
            ("route_prompt_contract", ("prompt_contract_hash",)),
        ),
    )
    project_id = _dispatch_string(
        payload,
        names=("project_id",),
        nested_keys=(
            ("scope", ("project_id", "project")),
            ("selected_scope", ("project_id", "project")),
            ("worker_contract", ("project_id",)),
        ),
    )
    governance_project_id = _dispatch_string(
        payload,
        names=("governance_project_id", "backlog_project_id"),
        nested_keys=(
            ("scope", ("governance_project_id", "backlog_project_id")),
            ("selected_scope", ("governance_project_id", "backlog_project_id")),
            ("worker_contract", ("governance_project_id",)),
            ("graph_identity", ("governance_project_id",)),
        ),
    ) or project_id
    target_project_id = _dispatch_string(
        payload,
        names=("target_project_id", "graph_project_id"),
        nested_keys=(
            ("scope", ("target_project_id", "graph_project_id")),
            ("selected_scope", ("target_project_id", "graph_project_id")),
            ("worker_contract", ("target_project_id",)),
            ("graph_identity", ("target_project_id",)),
        ),
    ) or project_id
    target_project_root = _dispatch_string(
        payload,
        names=("target_project_root", "target_graph_root"),
        nested_keys=(
            ("scope", ("target_project_root", "target_graph_root")),
            ("selected_scope", ("target_project_root", "target_graph_root")),
            ("worker_contract", ("target_project_root",)),
            ("graph_identity", ("target_project_root",)),
        ),
    )
    backlog_id = _dispatch_string(
        payload,
        names=("backlog_id",),
        nested_keys=(
            ("scope", ("backlog_id",)),
            ("selected_scope", ("backlog_id", "selected_backlog_id")),
            ("worker_contract", ("backlog_id",)),
        ),
    )
    task_id = _dispatch_string(
        payload,
        names=("task_id",),
        nested_keys=(
            ("runtime_identity", ("task_id",)),
            ("worker_contract", ("task_id",)),
        ),
    )
    parent_task_id = _dispatch_string(
        payload,
        names=("parent_task_id",),
        nested_keys=(
            ("runtime_identity", ("parent_task_id",)),
            ("worker_contract", ("parent_task_id",)),
            ("parent", ("task_id",)),
        ),
    )
    worker_role = _dispatch_string(
        payload,
        names=("worker_role", "role"),
        nested_keys=(
            ("runtime_identity", ("worker_role", "role")),
            ("worker_contract", ("worker_role", "role")),
        ),
    ) or MF_SUB_ROLE
    allocation_owner = _dispatch_string(
        payload,
        names=("allocation_owner", "observer_allocation_owner"),
        nested_keys=(
            ("runtime_identity", ("allocation_owner", "observer_allocation_owner")),
            ("branch_context", ("allocation_owner", "observer_allocation_owner")),
            ("worker_contract", ("allocation_owner", "observer_allocation_owner")),
        ),
    )
    worker_slot_id = _dispatch_string(
        payload,
        names=("worker_slot_id", "worker_id"),
        nested_keys=(
            ("runtime_identity", ("worker_slot_id", "worker_id")),
            ("branch_context", ("worker_slot_id", "worker_id")),
            ("worker_contract", ("worker_slot_id", "worker_id")),
        ),
    )
    values = {
        "branch": branch,
        "worktree": worktree,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "fence_token": fence_token,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
    }
    missing = [field for field in _DISPATCH_REQUIRED_FIELDS if not values[field]]
    if missing:
        raise MfSubagentContractError(
            "MF subagent dispatch missing required fields: " + ", ".join(missing)
        )

    owned_files = _string_list(
        payload.get("owned_files") or payload.get("write_scope"),
        field_name="owned_files",
    )
    if not owned_files:
        raise MfSubagentContractError("MF subagent dispatch missing owned_files fence")
    dirty_scope = _dirty_scope_evidence(payload.get("dirty_scope_check"))
    parent_route_lineage = _parent_route_lineage_from_payload(
        payload,
        project_id=project_id,
        backlog_id=backlog_id,
    )
    child_route_prompt_contract = _child_route_prompt_contract(
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    governed_evidence_required = _governed_nontrivial_graph_required(
        payload,
        parent_route_lineage=parent_route_lineage,
    )
    if governed_evidence_required and worker_role != MF_SUB_ROLE:
        raise MfSubagentContractError(
            "governed mf_sub dispatch requires worker_role=mf_sub"
        )
    graph_trace_evidence = _normalize_graph_trace_evidence(
        payload,
        required=governed_evidence_required,
        task_id=task_id,
        parent_task_id=parent_task_id,
        worker_role=worker_role,
        fence_token=fence_token,
    )
    branch_runtime_evidence = _normalize_branch_runtime_evidence(
        payload,
        required=governed_evidence_required,
        task_id=task_id,
        parent_task_id=parent_task_id,
        fence_token=fence_token,
        worktree_path=worktree,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=merge_queue_id,
    )
    service_dispatch_evidence = _normalize_service_dispatch_evidence(
        payload,
        required=governed_evidence_required,
    )

    policy = _nested_mapping(payload, "worktree_policy")
    override = _nested_mapping(payload, "same_worktree_override")
    if not override:
        override = _nested_mapping(payload, "override_policy")
    same_worktree_allowed = _bool(
        payload.get("same_worktree_allowed")
        or policy.get("same_worktree_allowed")
        or override.get("same_worktree_allowed")
    )
    normalized_worktree = _normalize_worktree_path(worktree)
    target_paths = [
        _normalize_worktree_path(path)
        for path in (
            target_worktree_path,
            main_worktree_path,
            _string(payload.get("target_worktree_path")),
            _string(payload.get("main_worktree_path")),
            _string(policy.get("target_worktree_path")),
            _string(policy.get("main_worktree_path")),
        )
        if _string(path)
    ]
    target_role = _string(
        payload.get("worktree_role") or policy.get("worktree_role")
    ).lower()
    same_worktree = normalized_worktree in target_paths or target_role in {
        "target",
        "main",
    }
    if same_worktree and not same_worktree_allowed:
        raise MfSubagentContractError(
            "same-worktree dispatch is blocked by default for local mf_sub workers"
        )

    override_used = same_worktree and same_worktree_allowed
    override_reason = ""
    if override_used:
        override_reason = _override_reason(payload, override)
        if not override_reason:
            raise MfSubagentContractError(
                "same-worktree dispatch override requires explicit operator reason"
            )
        if not dirty_scope["dirty_scope_exact_match"]:
            raise MfSubagentContractError(
                "same-worktree dispatch override requires dirty_scope_exact_match evidence"
            )
        if not _timeline_evidence_present(payload, override):
            raise MfSubagentContractError(
                "same-worktree dispatch override requires observer timeline evidence"
            )

    return {
        "schema_version": DISPATCH_GATE_SCHEMA_VERSION,
        "allowed": True,
        "role": MF_SUB_ROLE,
        "dispatch_default": DISPATCH_DEFAULT,
        "worktree_policy": WORKTREE_POLICY_MODE,
        "project_id": project_id,
        "governance_project_id": governance_project_id,
        "target_project_id": target_project_id,
        "target_project_root": target_project_root,
        "backlog_id": backlog_id,
        "allocation_owner": allocation_owner,
        "observer_allocation_owner": allocation_owner,
        "worker_slot_id": worker_slot_id,
        "branch": branch,
        "worktree": worktree,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "fence_token": fence_token,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "route_prompt_contract": child_route_prompt_contract,
        "parent_route_lineage": parent_route_lineage,
        "route_lineage": _mf_subagent_route_lineage(
            parent_route_lineage=parent_route_lineage,
            child_route_prompt_contract=child_route_prompt_contract,
        ),
        "governed_evidence_required": governed_evidence_required,
        "graph_trace_evidence": graph_trace_evidence,
        "branch_runtime_evidence": branch_runtime_evidence,
        "service_dispatch_evidence": service_dispatch_evidence,
        "owned_files": owned_files,
        "isolated_worktree": not same_worktree,
        "same_worktree_allowed": same_worktree_allowed,
        "override": {
            "used": override_used,
            "reason": override_reason,
            "timeline_evidence_recorded": override_used,
        },
        "dirty_scope_check": dirty_scope,
    }


def _require_context(context: BranchTaskRuntimeContext) -> None:
    missing = [field for field in _REQUIRED_CONTEXT_FIELDS if not getattr(context, field)]
    if missing:
        raise MfSubagentContractError(
            f"MF subagent context missing required fields: {', '.join(missing)}"
        )


def build_mf_subagent_input(
    context: BranchTaskRuntimeContext,
    *,
    prompt: str,
    acceptance_criteria: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    test_commands: Sequence[str] | None = None,
    operator_notes: str = "",
    backend: str = "codex_subagent",
    route_context_hash: str = "",
    prompt_contract_id: str = "",
    prompt_contract_hash: str = "",
    parent_route_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable input payload for a branch-isolated MF subagent."""

    _require_context(context)
    parent_task_id = _parent_task_id_for_contract_view(context)
    runtime_context_id = mf_subagent_runtime_context_id(context)
    child_route_prompt_contract = _child_route_prompt_contract(
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    normalized_parent_route_lineage = _normalize_parent_route_lineage(
        parent_route_lineage,
        required=parent_route_lineage is not None,
        project_id=context.project_id,
        backlog_id=context.backlog_id,
    )
    agent_task_contract = build_observer_owned_agent_task_contract(
        context,
        route_identity=child_route_prompt_contract,
        contract_version="mf_parallel.v1",
        required_evidence=_DEFAULT_RUNTIME_CONTRACT_EVIDENCE,
        target_files=target_files,
        target_fences=[context.fence_token],
        lifecycle_state=context.status,
    )
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "backend": backend,
        "backend_contract": BACKEND_CONTRACT,
        "project_id": context.project_id,
        "governance_project_id": context.governance_project_id or context.project_id,
        "target_project_id": context.target_project_id or context.project_id,
        "target_project_root": context.target_project_root,
        "task_id": context.task_id,
        "batch_id": context.batch_id,
        "backlog_id": context.backlog_id,
        "branch": {
            "branch_ref": context.branch_ref,
            "ref_name": context.ref_name,
            "worktree_id": context.worktree_id,
            "worktree_path": context.worktree_path,
            "base_commit": context.base_commit,
            "head_commit": context.head_commit,
            "target_head_commit": context.target_head_commit,
        },
        "runtime_identity": {
            "required_fields": [
                "task_id",
                "parent_task_id",
                "worker_role",
                "fence_token",
            ],
            "runtime_context_id": runtime_context_id,
            "project_id": context.project_id,
            "task_id": context.task_id,
            "parent_task_id": parent_task_id,
            "backlog_id": context.backlog_id,
            "worker_role": MF_SUB_ROLE,
            "agent_id": context.agent_id,
            "worker_id": context.worker_id,
            "worker_slot_id": context.worker_slot_id or context.worker_id,
            "allocation_owner": context.allocation_owner or context.agent_id,
            "observer_allocation_owner": context.allocation_owner or context.agent_id,
            "actual_host_worker_id": context.actual_host_worker_id,
            "host_startup_id": context.host_startup_id,
            "host_session_id": context.host_session_id,
            "attempt": context.attempt,
            "lease_id": context.lease_id,
            "fence_token": context.fence_token,
            "checkpoint_id": context.checkpoint_id,
            "depends_on": list(context.depends_on),
        },
        "chain_identity": {
            "chain_id": context.chain_id,
            "root_task_id": context.root_task_id,
            "stage_task_id": context.stage_task_id,
            "stage_type": context.stage_type,
            "retry_round": context.retry_round,
        },
        "graph_identity": {
            "governance_project_id": context.governance_project_id or context.project_id,
            "target_project_id": context.target_project_id or context.project_id,
            "target_project_root": context.target_project_root,
            "snapshot_id": context.snapshot_id,
            "projection_id": context.projection_id,
            "merge_queue_id": context.merge_queue_id,
            "merge_preview_id": context.merge_preview_id,
            "rollback_epoch": context.rollback_epoch,
            "replay_epoch": context.replay_epoch,
        },
        "work": {
            "prompt": prompt,
            "acceptance_criteria": _string_list(
                acceptance_criteria, field_name="acceptance_criteria"
            ),
            "target_files": _string_list(target_files, field_name="target_files"),
            "test_commands": _string_list(test_commands, field_name="test_commands"),
            "operator_notes": operator_notes,
        },
        "route_prompt_contract": child_route_prompt_contract,
        "parent_route_lineage": normalized_parent_route_lineage,
        "route_lineage": _mf_subagent_route_lineage(
            parent_route_lineage=normalized_parent_route_lineage,
            child_route_prompt_contract=child_route_prompt_contract,
        ),
        "agent_task_contract": agent_task_contract,
        "verification_route_policy": verification_route_policy_from_contract(
            {
                "route_identity": child_route_prompt_contract,
                "target_files": list(agent_task_contract["target_files"]),
            }
        ),
        "capabilities": {
            "can": list(MF_SUB_ALLOWED_CAPABILITIES),
            "cannot": list(MF_SUB_FORBIDDEN_ACTIONS),
        },
        "prechecks": {
            "asset_binding_proposal": {
                "proposal_schema_version": ASSET_BINDING_PROPOSAL_SCHEMA_VERSION,
                "precheck_schema_version": ASSET_BINDING_PRECHECK_SCHEMA_VERSION,
                "local_function": (
                    "agent.governance.asset_binding_proposals."
                    "precheck_asset_binding_proposal"
                ),
                "gate_rule": (
                    "Run the same precheck on any doc/test/config binding proposal "
                    "before submitting it; include the compact self_precheck object "
                    "with the proposal so the server gate can verify the hash."
                ),
            },
        },
        "required_output": list(MF_SUB_REQUIRED_OUTPUT),
    }


def normalize_mf_subagent_result(
    payload: Mapping[str, Any],
    *,
    expected_fence_token: str,
) -> dict[str, Any]:
    """Validate and normalize a branch worker result before queueing merge review."""

    if not expected_fence_token:
        raise MfSubagentContractError("expected_fence_token is required")
    missing = [field for field in MF_SUB_REQUIRED_OUTPUT if field not in payload]
    if missing:
        raise MfSubagentContractError(
            f"MF subagent result missing required fields: {', '.join(missing)}"
        )

    fence_token = str(payload.get("fence_token") or "")
    if fence_token != expected_fence_token:
        raise MfSubagentContractError("MF subagent result fence token is stale")

    actions = {action.lower() for action in _string_list(payload.get("actions"), field_name="actions")}
    for field_name, action in _FORBIDDEN_RESULT_FLAGS.items():
        if payload.get(field_name):
            actions.add(action)
    forbidden = sorted(actions.intersection(MF_SUB_FORBIDDEN_ACTIONS))
    if forbidden:
        raise MfSubagentContractError(
            f"MF subagent result attempted forbidden actions: {', '.join(forbidden)}"
        )

    status = str(payload.get("status") or "")
    changed_files = _string_list(payload.get("changed_files"), field_name="changed_files")
    new_files = _string_list(payload.get("new_files"), field_name="new_files")
    blockers = _string_list(payload.get("blockers"), field_name="blockers")
    test_results = _mapping(payload.get("test_results"), field_name="test_results")
    test_status = str(test_results.get("status") or "").lower()
    tests_passed = bool(test_results.get("passed")) or test_status in _PASS_STATUSES
    merge_queue_ready = status in _READY_STATUSES and tests_passed and not blockers

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "status": status,
        "changed_files": changed_files,
        "new_files": new_files,
        "test_results": test_results,
        "checkpoint_id": str(payload.get("checkpoint_id") or ""),
        "fence_token": fence_token,
        "merge_queue_ready": merge_queue_ready,
        "blockers": blockers,
        "summary": str(payload.get("summary") or ""),
        "evidence": _mapping(payload.get("evidence"), field_name="evidence"),
    }


def _status_short_files(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = value.splitlines()
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        lines = [str(item) for item in value]
    else:
        return []
    files: list[str] = []
    for line in lines:
        text = str(line or "").strip("\n")
        if not text.strip():
            continue
        path = text[3:] if len(text) > 3 else text
        files.append(path.strip())
    return files


def _finish_status_files(payload: Mapping[str, Any], *keys: str) -> list[str]:
    for key in keys:
        files = _status_short_files(payload.get(key))
        if files:
            return files
    return []


def _finish_nested_status_files(payload: Mapping[str, Any], key: str) -> list[str]:
    source = _nested_mapping(payload, key)
    if not source:
        return []
    return (
        _status_short_files(source.get("status_short"))
        or _status_short_files(source.get("short"))
        or _string_list_from_mapping(source, "dirty_files", "changed_files", "files")
    )


def _finish_owned_files(payload: Mapping[str, Any]) -> list[str]:
    return _dedupe_strings(
        _string_list_from_mapping(
            payload,
            "owned_files",
            "target_files",
            "write_scope",
        )
    )


def _validate_finish_scope_precheck(
    payload: Mapping[str, Any],
    *,
    context: BranchTaskRuntimeContext,
    changed_files: Sequence[str],
    new_files: Sequence[str],
) -> dict[str, Any]:
    parent_files = (
        _finish_status_files(
            payload,
            "parent_main_status_short",
            "main_worktree_status_short",
            "parent_worktree_status_short",
        )
        or _finish_nested_status_files(payload, "parent_main_status")
        or _finish_nested_status_files(payload, "main_worktree_status")
        or _finish_nested_status_files(payload, "parent_checkout")
    )
    if parent_files:
        raise MfSubagentContractError(
            "MF subagent finish gate requires parent/main checkout clean"
        )

    worker_files = (
        _finish_status_files(
            payload,
            "worker_worktree_status_short",
            "worktree_status_short",
            "worker_status_short",
        )
        or _finish_nested_status_files(payload, "worker_worktree_status")
        or _finish_nested_status_files(payload, "worktree_diff_scope")
    )
    claimed_files = _dedupe_strings([*changed_files, *new_files])
    owned_files = _finish_owned_files(payload)
    if owned_files:
        outside_owned = sorted(set(claimed_files).difference(owned_files))
        if outside_owned:
            raise MfSubagentContractError(
                "MF subagent finish gate changed files outside owned file fence: "
                + ", ".join(outside_owned)
            )
    if worker_files:
        unclaimed_worker_files = sorted(set(worker_files).difference(claimed_files))
        if unclaimed_worker_files:
            raise MfSubagentContractError(
                "MF subagent finish gate worker worktree status has unclaimed files: "
                + ", ".join(unclaimed_worker_files)
            )
        if owned_files:
            outside_worker_owned = sorted(set(worker_files).difference(owned_files))
            if outside_worker_owned:
                raise MfSubagentContractError(
                    "MF subagent finish gate worker worktree changed files outside owned "
                    "file fence: " + ", ".join(outside_worker_owned)
                )
    actual_cwd = _string(payload.get("actual_cwd"))
    actual_git_root = _string(payload.get("actual_git_root") or payload.get("git_root"))
    expected_worktree = _normalize_worktree_path(context.worktree_path)
    cwd_ok = True
    git_root_ok = True
    if actual_cwd:
        cwd_ok = _normalize_worktree_path(actual_cwd) == expected_worktree
    if actual_git_root:
        git_root_ok = _normalize_worktree_path(actual_git_root) == expected_worktree
    if not cwd_ok or not git_root_ok:
        raise MfSubagentContractError(
            "MF subagent finish gate requires actual_cwd and actual_git_root to "
            "match assigned worktree"
        )
    return {
        "schema_version": "mf_subagent_finish_scope_precheck.v1",
        "parent_main_clean": True,
        "parent_main_status_short": [],
        "worker_worktree_files": worker_files,
        "claimed_files": claimed_files,
        "owned_files": owned_files,
        "owned_file_scope_passed": True,
        "assigned_worktree": context.worktree_path,
        "actual_cwd": actual_cwd,
        "actual_git_root": actual_git_root,
        "cwd_matches_assigned_worktree": cwd_ok,
        "git_root_matches_assigned_worktree": git_root_ok,
    }


def validate_mf_subagent_finish_gate(
    payload: Mapping[str, Any],
    *,
    context: BranchTaskRuntimeContext,
) -> dict[str, Any]:
    """Validate a subagent finish claim against durable branch runtime facts.

    The subagent payload is a claim. This function only returns evidence that
    matches the current runtime context and is ready to become a checkpoint.
    """

    _require_context(context)
    normalized = normalize_mf_subagent_result(
        payload,
        expected_fence_token=context.fence_token,
    )
    if not normalized["merge_queue_ready"]:
        raise MfSubagentContractError("MF subagent finish gate is not merge-queue ready")

    identity_mismatches: list[str] = []
    for field in _FINISH_IDENTITY_FIELDS:
        claimed = str(payload.get(field) or "")
        expected = str(getattr(context, field) or "")
        if claimed and expected and claimed != expected:
            identity_mismatches.append(field)
    if identity_mismatches:
        raise MfSubagentContractError(
            "MF subagent finish gate identity mismatch: "
            + ", ".join(sorted(identity_mismatches))
        )

    claimed_head = str(payload.get("head_commit") or payload.get("branch_head") or "")
    checkpoint_id = str(normalized.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        raise MfSubagentContractError("checkpoint_id is required")
    finish_precheck = _validate_finish_scope_precheck(
        payload,
        context=context,
        changed_files=normalized["changed_files"],
        new_files=normalized["new_files"],
    )
    parent_route_lineage = _parent_route_lineage_from_payload(
        payload,
        project_id=context.project_id,
        backlog_id=context.backlog_id,
    )
    child_route_prompt_contract = _child_route_prompt_contract_from_payload(payload)
    parent_task_id = _dispatch_string(
        payload,
        names=("parent_task_id",),
        nested_keys=(
            ("runtime_identity", ("parent_task_id",)),
            ("worker_contract", ("parent_task_id",)),
            ("parent", ("task_id",)),
        ),
    )
    worker_role = _dispatch_string(
        payload,
        names=("worker_role", "role"),
        nested_keys=(
            ("runtime_identity", ("worker_role", "role")),
            ("worker_contract", ("worker_role", "role")),
        ),
    ) or MF_SUB_ROLE
    governed_evidence_required = _governed_nontrivial_graph_required(
        payload,
        parent_route_lineage=parent_route_lineage,
    )
    if governed_evidence_required and worker_role != MF_SUB_ROLE:
        raise MfSubagentContractError(
            "governed mf_sub finish requires worker_role=mf_sub"
        )
    graph_trace_evidence = _normalize_graph_trace_evidence(
        payload,
        required=governed_evidence_required,
        task_id=context.task_id,
        parent_task_id=parent_task_id,
        worker_role=worker_role,
        fence_token=context.fence_token,
    )
    read_receipt_hash = _dispatch_string(
        payload,
        names=("read_receipt_hash", "worker_read_receipt_hash"),
        nested_keys=(
            ("read_receipt", ("hash", "read_receipt_hash")),
            ("worker_contract", ("read_receipt_hash",)),
            ("evidence", ("read_receipt_hash",)),
        ),
    )
    gate_receipt_hash = _dispatch_string(
        payload,
        names=("gate_receipt_hash",),
        nested_keys=(
            ("gate_receipt", ("hash", "gate_receipt_hash")),
            ("worker_contract", ("gate_receipt_hash",)),
            ("evidence", ("gate_receipt_hash",)),
        ),
    )
    startup_evidence = _route_startup_evidence(payload)
    startup_present = _bounded_startup_evidence_present(startup_evidence)
    if not startup_present:
        raise MfSubagentContractError(
            "MF subagent finish gate requires actual mf_subagent_startup evidence "
            "before close-ready"
        )
    if not read_receipt_hash:
        raise MfSubagentContractError(
            "MF subagent finish gate requires mf_subagent_read_receipt before "
            "close-ready"
        )

    return {
        "schema_version": FINISH_GATE_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "project_id": context.project_id,
        "governance_project_id": context.governance_project_id or context.project_id,
        "target_project_id": context.target_project_id or context.project_id,
        "target_project_root": context.target_project_root,
        "task_id": context.task_id,
        "backlog_id": context.backlog_id,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "head_commit": claimed_head or context.head_commit,
        "checkpoint_id": checkpoint_id,
        "fence_token": context.fence_token,
        "replay_source": FINISH_GATE_REPLAY_SOURCE,
        "finish_precheck": finish_precheck,
        "route_prompt_contract": child_route_prompt_contract,
        "parent_route_lineage": parent_route_lineage,
        "route_lineage": _mf_subagent_route_lineage(
            parent_route_lineage=parent_route_lineage,
            child_route_prompt_contract=child_route_prompt_contract,
        ),
        "governed_evidence_required": governed_evidence_required,
        "startup_evidence": startup_evidence,
        "read_receipt_hash": read_receipt_hash,
        "gate_receipt_hash": gate_receipt_hash,
        "receipt_gate": {
            "schema_version": "mf_subagent_receipt_gate.v1",
            "read_receipt_required_before_counted_evidence": bool(
                governed_evidence_required or graph_trace_evidence.get("present")
            ),
            "read_receipt_present": bool(read_receipt_hash),
            "gate_receipt_present": bool(gate_receipt_hash),
            "startup_present": startup_present,
            "status": "passed",
        },
        "graph_trace_evidence": graph_trace_evidence,
        "changed_files": normalized["changed_files"],
        "new_files": normalized["new_files"],
        "test_results": normalized["test_results"],
        "blockers": normalized["blockers"],
        "summary": normalized["summary"],
        "evidence": normalized["evidence"],
        "merge_queue_ready": True,
        "close_ready": True,
    }
