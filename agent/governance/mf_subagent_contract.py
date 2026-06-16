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
FINISH_GATE_LANE_OWNERSHIP_PROJECTION_SCHEMA_VERSION = (
    "mf_subagent_finish_gate_lane_ownership_projection.v1"
)
FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION = (
    "mf_subagent_finish_gate_close_projection.v1"
)
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
META_CONTRACT_SCHEMA_VERSION = "meta_contract.v1"
META_CONTRACT_TIMELINE_GATE_SCHEMA_VERSION = "meta_contract_timeline_event_gate.v1"
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
_FINISH_WORKER_ATTESTATION_KEYS = (
    "finish_time_worker_self_attestation",
    "worker_self_attestation",
    "finish_attestation",
)
_FINISH_WORKER_ATTESTATION_PUBLIC_FIELDS = (
    "schema_version",
    "attestation_phase",
    "status",
    "ok",
    "worker_self_attesting",
    "self_attesting",
    "finish_time_self_attesting",
    "finish_time_blockers",
    "worker_session_id",
    "filer_principal",
    "worker_transcript_path",
    "worker_transcript_ref",
    "transcript_ref",
    "resolved_transcript_path",
    "harness_type",
    "blockers",
    "known_bad_playback_4178",
)
_GENERIC_OR_OBSERVER_WORKER_FILERS = {
    "api",
    "host-adapter",
    "host_adapter",
    "implementation_worker",
    "mf_sub",
    "mf_subagent",
    "observer",
    "subagent",
    "system",
    "worker",
}
_WORKER_HARNESS_TYPE_ALIASES = {
    "codex_builtin_subagent": "codex",
    "codex_built_in_subagent": "codex",
    "codex-built-in-subagent": "codex",
    "codex_builtin": "codex",
    "codex_cli": "codex",
    "claude_code": "claude",
    "claude-code": "claude",
}
_SUPPORTED_WORKER_HARNESS_TYPES = {"claude", "codex"}
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
    "route_token_ref",
    "route_context_hash",
    "prompt_contract_id",
)
_MF_SUBAGENT_ALLOWED_QUERY_PURPOSES = {
    "subagent_context_build",
    "subagent_gate_validation",
}
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
_DISPATCH_GRAPH_OBLIGATION_CONTAINER_KEYS = (
    "dispatch_graph_obligation",
    "graph_first_obligations",
    "graph_query_obligation",
    "mf_subagent_graph_first_obligations",
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
_META_CONTRACT_TEMPLATE = "meta_contract.v1.json"
_META_ROLE_ALIASES = {
    "observer": OBSERVER_COORDINATOR_ROLE,
    "mf_observer": OBSERVER_COORDINATOR_ROLE,
    "route_observer": OBSERVER_COORDINATOR_ROLE,
    "coordinator": OBSERVER_COORDINATOR_ROLE,
    "observer_coordinator": OBSERVER_COORDINATOR_ROLE,
    "judger": "judge",
    "judge": "judge",
    "system": "system",
    "service_router": "system",
    "service-router": "system",
    "route_gate": "system",
    "route-token-gate": "system",
    "route_token_gate": "system",
    "watchdog": "system",
    "observer_command_watchdog": "system",
    "mf_sub": MF_SUB_ROLE,
    "worker": MF_SUB_ROLE,
    "implementation_worker": MF_SUB_ROLE,
    "bounded_implementation_worker": MF_SUB_ROLE,
    "bounded_worker": MF_SUB_ROLE,
    "qa": "qa",
    "qa_reviewer": "qa",
    "qa_verifier": "qa",
    "independent_verifier": "qa",
    "independent_reviewer": "qa",
    "operator": "operator",
    "api": "operator",
}
_META_ACTION_ALIASES = {
    "route_context": "route_context",
    "route": "route_context",
    "route_action_precheck": "route_action_precheck",
    "route_precheck": "route_action_precheck",
    "observer_work_mode_transition": "observer_work_mode_transition",
    "bounded_implementation_worker_dispatch": "dispatch_bounded_worker",
    "mf_subagent_dispatch": "dispatch_bounded_worker",
    "dispatch_bounded_worker": "dispatch_bounded_worker",
    "dispatch": "dispatch_bounded_worker",
    "implementation": "implementation",
    "implement": "implementation",
    "worker_progress": "worker_progress",
    "progress": "worker_progress",
    "mf_subagent_session_token_reissue": "worker_progress",
    "mf_subagent_session_token_renew": "worker_progress",
    "session_token_reissue": "worker_progress",
    "session_token_renew": "worker_progress",
    "task_started": "worker_progress",
    "task_claimed": "worker_progress",
    "task_completed": "worker_progress",
    "task_succeeded": "worker_progress",
    "ai_implementation_evidence_proposed": "worker_progress",
    "gate_evidence_verified": "worker_progress",
    "gate_result": "worker_progress",
    "observation": "worker_progress",
    "patch": "patch",
    "apply_patch": "patch",
    "mf_test_scenario_decision": "observer_command",
    "scenario_spec": "observer_command",
    "mf_process_timeline": "observer_command",
    "mf_subagent_read_receipt": "read_receipt",
    "mf_subagent_read_receipt_v1": "read_receipt",
    "read_receipt": "read_receipt",
    "startup_read_receipt": "read_receipt",
    "mf_subagent_startup": "mf_subagent_startup",
    "mf_subagent_startup_gate": "mf_subagent_startup",
    "mf_subagent_startup_adoption": "mf_subagent_startup",
    "startup_gate": "mf_subagent_startup",
    "mf_subagent_finish_gate": "review_ready",
    "finish_gate": "review_ready",
    "review_ready": "review_ready",
    "waiting_merge": "review_ready",
    "close_ready": "close_ready",
    "close_after_clauses": "close_after_clauses",
    "close_after_clause": "close_after_clauses",
    "backlog_close": "close_after_clauses",
    "merge": "merge",
    "merge_preview": "merge_preview",
    "merge_queue_entry": "merge_queue_entry",
    "live_merge": "live_merge",
    "reconcile": "reconcile",
    "scope_reconcile": "reconcile",
    "graph_reconcile": "reconcile",
    "record_blocker": "record_blocker",
    "blocker": "record_blocker",
    "blocker_recorded": "record_blocker",
    "observer_command": "observer_command",
    "observer_command_complete": "observer_command",
    "observer_command_disposition": "observer_command",
    "independent_verification": "independent_verification",
    "verification": "qa_verification",
    "qa_verification": "qa_verification",
    "qa_review": "qa_review",
    "hotfix_entered": "hotfix_entered",
    "hotfix_enter": "hotfix_entered",
    "hotfix": "hotfix_entered",
    "hotfix_backlog_close": "hotfix_under_action",
    "hotfix_under_action": "hotfix_under_action",
    "route_token_gate": "route_token_gate",
    "route_token_gate_project_bootstrap_refusal": "route_token_gate",
    "route_waiver": "route_token_gate",
    "route_waiver_recorded": "route_token_gate",
    "route_identity_cleanup": "route_identity_cleanup",
    "route_identity": "route_identity_cleanup",
    "route_action_source_event": "service_route",
    "route_context_source_event": "service_route",
    "route_source_event": "service_route",
    "service_route": "service_route",
    "service_route_completed": "service_route",
    "service_route_blocked": "service_route",
    "no_progress_timeout": "no_progress_timeout",
    "observer_command_no_progress_timeout": "no_progress_timeout",
    "mf_timeline_gate_bypass_rejected": "forbidden_attempt_recorded",
    "bypass_rejected": "forbidden_attempt_recorded",
    "forbidden_attempt_recorded": "forbidden_attempt_recorded",
    "stale_artifact_cleanup": "stale_artifact_cleanup",
    "governance_stale_artifact_cleanup_apply": "stale_artifact_cleanup",
    "graph_trace": "graph_trace",
    "graph_query_trace": "graph_trace",
}
_META_WORK_MODE_TRANSITION_ROUTE_IDENTITY_KEYS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
)
_META_WORK_MODE_TRANSITION_PRECHECK_REF_KEYS = (
    "route_action_precheck_event_id",
    "route_action_precheck_event_ref",
    "route_action_precheck_ref",
    "route_action_precheck_timeline_id",
    "bound_route_action_precheck_event_id",
    "bound_route_action_precheck_ref",
)
_META_OBSERVER_ROLE_TOKENS = {"observer", "coordinator"}
_META_JUDGE_ROLE_TOKENS = {"judger", "judge"}
_META_QA_ROLE_TOKENS = {"qa", "verifier", "reviewer"}
_META_WORKER_ROLE_TOKENS = {"mf_sub", "subagent", "worker"}
_META_SYSTEM_ROLE_TOKENS = {"system", "service_router", "route_gate", "watchdog"}
_META_ON_BEHALF_KEYS = (
    "on_behalf_of",
    "filed_on_behalf_by",
    "recorded_on_behalf_of",
    "reviewer",
)
_META_SELF_ATTESTING_KEYS = (
    "self_attesting",
    "worker_self_attesting",
    "self_filed",
)
_META_ALWAYS_FORBIDDEN_FLAG_KEYS = (
    "bypass_timeline_gate",
    "self_waiver",
    "self_clear_judge_blocker",
    "self_fix_and_close",
    "fork_identity_to_launder",
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


# Format regex: "sha256:" followed by exactly 64 lowercase hex characters.
_SHA256_HASH_FORMAT = "sha256:"
_SHA256_HEX_LEN = 64
_HEX_CHARS = frozenset("0123456789abcdef")


def _is_valid_sha256_format(claimed: str) -> bool:
    """Return True when *claimed* matches the sha256:<64 lowercase hex> format floor."""
    if not claimed.startswith(_SHA256_HASH_FORMAT):
        return False
    hex_part = claimed[len(_SHA256_HASH_FORMAT):]
    if len(hex_part) != _SHA256_HEX_LEN:
        return False
    return all(c in _HEX_CHARS for c in hex_part)


def verify_content_hash(
    claimed_hash: str,
    obj: Any,
    *,
    field_name: str = "hash",
) -> dict[str, Any]:
    """Verify a claimed sha256 content hash.

    Two-tier check:
    1. FORMAT FLOOR: *claimed_hash* must match ``sha256:<64 lowercase hex>``.
       Forged/copied junk (e.g. ``sha256:rr-...``, uppercase hex, wrong length)
       is rejected here regardless of whether *obj* is present.
    2. CONTENT VERIFY: when *obj* is not None, recompute
       ``canonical_contract_hash(obj)`` (same stable serialization as the
       producer) and require equality with *claimed_hash*.  Mismatch →
       structured ``content_hash_mismatch`` refusal.  Object absent → format
       floor only (presence semantics unchanged for backward compatibility).

    Returns a dict with keys:
    - ``ok`` (bool)
    - ``status``: ``"verified"`` | ``"format_error"`` | ``"content_hash_mismatch"``
    - ``field_name``
    - ``claimed_hash``
    - ``object_present`` (bool)
    - ``computed_hash`` (only when object present)
    """
    result_base: dict[str, Any] = {
        "field_name": field_name,
        "claimed_hash": claimed_hash,
        "object_present": obj is not None,
    }
    if not _is_valid_sha256_format(claimed_hash):
        return {
            **result_base,
            "ok": False,
            "status": "format_error",
            "reason": (
                f"{field_name} must be sha256:<64 lowercase hex>; "
                f"got: {claimed_hash!r}"
            ),
        }
    if obj is None:
        # Format floor passed; object absent — presence semantics unchanged.
        return {
            **result_base,
            "ok": True,
            "status": "format_verified",
        }
    computed = canonical_contract_hash(obj)
    if computed != claimed_hash:
        return {
            **result_base,
            "ok": False,
            "status": "content_hash_mismatch",
            "computed_hash": computed,
            "reason": (
                f"{field_name} content mismatch: "
                f"claimed {claimed_hash!r} != computed {computed!r}"
            ),
        }
    return {
        **result_base,
        "ok": True,
        "status": "verified",
        "computed_hash": computed,
    }


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
    observer_command_id = _string(latest_revision_payload.get("observer_command_id"))
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
        "observer_command_id": observer_command_id,
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
                "runtime_context_id",
                "fence_token",
                "session_token",
                "target_project_root",
            ],
            "server_resolved_context_fields": [
                "task_id",
                "parent_task_id",
                "worker_role",
                "governance_project_id",
                "target_project_id",
            ],
            "route_identity_fields": [
                "route_id",
                "route_context_hash",
                "prompt_contract_id",
                "prompt_contract_hash",
                "route_token_ref",
                "visible_injection_manifest_hash",
            ],
            "trace_ids_required_in_timeline": True,
        },
        "read_receipt_ordering": {
            "schema_version": "mf_subagent_read_receipt_ordering.v1",
            "timeline_event_kind": "mf_subagent_read_receipt",
            "observer_command_id": observer_command_id,
            "required_command_type": "execute_backlog_row",
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
        "observer_command": {
            "schema_version": "mf_subagent_observer_command_lineage.v1",
            "required": True,
            "observer_command_id": observer_command_id,
            "required_command_type": "execute_backlog_row",
            "backlog_id": context.backlog_id,
            "claim_rule": (
                "Use the claimed backlog-specific execute_backlog_row command id; "
                "do not substitute an unrelated observer_command_next result."
            ),
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
        "observer_command_id": observer_command_id,
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
            "observer_command_id": observer_command_id,
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


def _route_identity_value(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        token = _string(payload.get(name))
        if token:
            return token
    for container in _route_identity_containers(payload):
        for name in names:
            token = _string(container.get(name))
            if token:
                return token
        hashes = _nested_mapping(container, "hashes")
        for name in names:
            token = _string(hashes.get(name))
            if token:
                return token
    return ""


def _route_id(payload: Mapping[str, Any]) -> str:
    return _route_identity_value(payload, "route_id", "id")


def _route_token_ref(payload: Mapping[str, Any]) -> str:
    return _route_identity_value(payload, "route_token_ref", "route_token_reference")


def _route_visible_injection_manifest_hash(payload: Mapping[str, Any]) -> str:
    return _route_identity_value(
        payload,
        "visible_injection_manifest_hash",
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


def _lineage_list_field_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return True
    return bool(_string(value))


def _lineage_first_present_list_value(
    *sources: tuple[Mapping[str, Any], Sequence[str]],
) -> tuple[Any, bool]:
    for source, field_names in sources:
        for field_name in field_names:
            if field_name not in source:
                continue
            value = source.get(field_name)
            if _lineage_list_field_present(value):
                return value, True
    return None, False


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

    allowed_actions_value, allowed_actions_present = _lineage_first_present_list_value(
        (packet, ("allowed_actions",)),
        (route_machine_context, ("allowed_actions",)),
    )
    blocked_actions_value, blocked_actions_present = _lineage_first_present_list_value(
        (packet, ("blocked_actions",)),
        (route_machine_context, ("blocked_actions",)),
    )
    required_lanes_value, required_lanes_present = _lineage_first_present_list_value(
        (packet, ("required_lanes",)),
        (route_machine_context, ("required_lanes",)),
    )
    required_evidence_value, required_evidence_present = (
        _lineage_first_present_list_value(
            (
                packet,
                (
                    "required_evidence",
                    "required_evidence_ids",
                    "evidence_required",
                    "evidence_requirements",
                ),
            ),
            (
                route_machine_context,
                (
                    "required_evidence",
                    "required_evidence_ids",
                    "evidence_required",
                ),
            ),
        )
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
        "allowed_actions": _lineage_text_list(allowed_actions_value),
        "blocked_actions": _lineage_text_list(blocked_actions_value),
        "required_lanes": _lineage_text_list(required_lanes_value),
        "required_evidence": _lineage_text_list(required_evidence_value),
    }
    required_list_fields_present = {
        "allowed_actions": allowed_actions_present,
        "blocked_actions": blocked_actions_present,
        "required_lanes": required_lanes_present,
        "required_evidence": required_evidence_present,
    }

    missing = [
        field
        for field in _PARENT_ROUTE_LINEAGE_REQUIRED_FIELDS
        if (
            not required_list_fields_present[field]
            if field in required_list_fields_present
            else not normalized[field]
        )
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
    allow_payload_fallback: bool | None = None,
) -> dict[str, Any]:
    source = _graph_trace_source(
        payload,
        allow_payload_fallback=(
            not required if allow_payload_fallback is None else allow_payload_fallback
        ),
    )
    trace_ids = _dedupe_strings(
        trace_id
        for item in _deep_field_values(source, _GRAPH_TRACE_ID_KEYS)
        for trace_id in _trace_id_strings(item)
    )
    query_source = _first_deep_string(source, {"query_source"})
    query_purpose = _first_deep_string(source, {"query_purpose"})
    if query_source and query_source != "mf_subagent":
        raise MfSubagentContractError(
            "graph trace evidence must use query_source=mf_subagent"
        )
    if required and not query_source:
        raise MfSubagentContractError(
            "graph trace evidence requires explicit query_source=mf_subagent"
        )
    if query_purpose and query_purpose not in _MF_SUBAGENT_ALLOWED_QUERY_PURPOSES:
        raise MfSubagentContractError(
            "graph trace evidence uses unsupported query_purpose="
            f"{query_purpose}; use subagent_context_build or subagent_gate_validation"
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

    source_name = _string(source.get("source")).lower()
    source_details = _mapping(
        source.get("source_details"),
        field_name="graph_trace_evidence.source_details",
    )
    db_verified = _bool(source.get("db_verified"))
    missing_trace_ids = _string_list(
        source.get("missing_trace_ids"),
        field_name="graph_trace_evidence.missing_trace_ids",
    )
    identity_mismatches = source.get("identity_mismatches")
    identity_mismatch_count = 0
    if isinstance(identity_mismatches, Mapping):
        identity_mismatch_count = 1
    elif isinstance(identity_mismatches, Sequence) and not isinstance(
        identity_mismatches, (str, bytes, bytearray)
    ):
        identity_mismatch_count = len(identity_mismatches)
    if not db_verified and _bool(source_details.get("graph_query_traces")):
        db_verified = bool(trace_ids) and not missing_trace_ids and not identity_mismatch_count
    if required:
        if source_name != "graph_query_traces":
            raise MfSubagentContractError(
                "graph trace evidence must be rederived from graph_query_traces"
            )
        if not db_verified:
            raise MfSubagentContractError(
                "graph trace evidence must be verified against governance DB"
            )
        if missing_trace_ids:
            raise MfSubagentContractError(
                "graph trace evidence references trace ids missing from governance DB"
            )
        if identity_mismatch_count:
            raise MfSubagentContractError(
                "graph trace evidence identity does not match mf_sub lane"
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
        "query_purpose": query_purpose,
        "trace_ids": trace_ids,
        "task_id": observed["task_id"],
        "parent_task_id": observed["parent_task_id"],
        "worker_role": observed["worker_role"],
        "fence_token": observed["fence_token"],
        "source": source_name,
        "db_verified": db_verified,
        "missing_trace_ids": missing_trace_ids,
        "identity_mismatches": identity_mismatches
        if isinstance(identity_mismatches, (Mapping, list, tuple))
        else [],
    }


def _dispatch_graph_obligation_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in _DISPATCH_GRAPH_OBLIGATION_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _normalize_dispatch_graph_obligation(
    payload: Mapping[str, Any],
    *,
    required: bool,
    task_id: str = "",
    parent_task_id: str = "",
    worker_role: str = "",
    fence_token: str = "",
) -> dict[str, Any]:
    source = _dispatch_graph_obligation_source(payload)
    query = _nested_mapping(source, "query")
    identity_source: Mapping[str, Any] = query if query else source
    query_source = _first_deep_string(identity_source, {"query_source"})
    query_purpose = _first_deep_string(identity_source, {"query_purpose"})
    embedded_task_id = _first_deep_string(identity_source, {"task_id"})
    embedded_parent_task_id = _first_deep_string(identity_source, {"parent_task_id"})
    embedded_worker_role = _first_deep_string(identity_source, {"worker_role", "role"})
    embedded_fence_token = _first_deep_string(identity_source, {"fence_token"})
    read_receipt_before = _string_list(
        source.get("read_receipt_required_before"),
        field_name="read_receipt_required_before",
    )
    trace_schema_version = _first_deep_string(
        source,
        {
            "trace_evidence_schema_version",
            "finish_gate_trace_evidence_schema_version",
        },
    )

    if required and not source:
        raise MfSubagentContractError(
            "governed mf_sub dispatch requires graph-first obligation evidence"
        )
    if query_source and query_source != "mf_subagent":
        raise MfSubagentContractError(
            "dispatch graph obligation must use query_source=mf_subagent"
        )
    if required and not query_source:
        raise MfSubagentContractError(
            "dispatch graph obligation requires query_source=mf_subagent"
        )
    if query_purpose and query_purpose not in _MF_SUBAGENT_ALLOWED_QUERY_PURPOSES:
        raise MfSubagentContractError(
            "dispatch graph obligation uses unsupported query_purpose="
            f"{query_purpose}; use subagent_context_build or subagent_gate_validation"
        )
    if embedded_worker_role and embedded_worker_role != MF_SUB_ROLE:
        raise MfSubagentContractError(
            "dispatch graph obligation must use worker_role=mf_sub"
        )
    if (
        trace_schema_version
        and trace_schema_version != GRAPH_TRACE_SCHEMA_VERSION
    ):
        raise MfSubagentContractError(
            "dispatch graph obligation must point finish evidence to "
            f"{GRAPH_TRACE_SCHEMA_VERSION}"
        )

    observed = {
        "task_id": embedded_task_id or ("" if required else task_id),
        "parent_task_id": embedded_parent_task_id
        or ("" if required else parent_task_id),
        "worker_role": embedded_worker_role
        or ("" if required else worker_role or MF_SUB_ROLE),
        "fence_token": embedded_fence_token or ("" if required else fence_token),
    }
    expected = {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_role": MF_SUB_ROLE if required else worker_role or MF_SUB_ROLE,
        "fence_token": fence_token,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if expected_value and observed[field] and observed[field] != expected_value
    ]
    if mismatches:
        raise MfSubagentContractError(
            "dispatch graph obligation identity mismatch: "
            + ", ".join(sorted(mismatches))
        )
    missing_context = [
        field
        for field, value in observed.items()
        if field in {"task_id", "parent_task_id", "worker_role", "fence_token"}
        and not value
    ]
    if required and missing_context:
        raise MfSubagentContractError(
            "dispatch graph obligation missing required context: "
            + ", ".join(sorted(missing_context))
        )
    if required and "graph_query" not in read_receipt_before:
        raise MfSubagentContractError(
            "dispatch graph obligation must require read receipt before graph_query"
        )

    return {
        "schema_version": "mf_subagent_dispatch_graph_obligation.v1",
        "required": required,
        "present": bool(source),
        "counts_as_worker_graph_trace_evidence": False,
        "finish_gate_requires_worker_graph_trace": bool(required),
        "query_source": query_source,
        "query_purpose": query_purpose,
        "task_id": observed["task_id"],
        "parent_task_id": observed["parent_task_id"],
        "worker_role": observed["worker_role"],
        "fence_token": observed["fence_token"],
        "read_receipt_required_before": read_receipt_before,
        "trace_evidence_schema_version": (
            trace_schema_version or GRAPH_TRACE_SCHEMA_VERSION
        ),
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
    route_id: str = "",
    route_context_hash: str = "",
    prompt_contract_id: str = "",
    prompt_contract_hash: str = "",
    route_token_ref: str = "",
    visible_injection_manifest_hash: str = "",
) -> dict[str, str]:
    contract = {
        "route_context_hash": _string(route_context_hash),
        "prompt_contract_id": _string(prompt_contract_id),
        "prompt_contract_hash": _string(prompt_contract_hash),
    }
    for key, value in (
        ("route_id", route_id),
        ("route_token_ref", route_token_ref),
        ("visible_injection_manifest_hash", visible_injection_manifest_hash),
    ):
        token = _string(value)
        if token:
            contract[key] = token
    return contract


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
        route_id=_route_id(payload) or _string(child_route.get("route_id")),
        route_context_hash=_route_context_hash(payload)
        or _string(child_route.get("route_context_hash")),
        prompt_contract_id=_route_prompt_contract_id(payload)
        or _string(child_route.get("prompt_contract_id") or child_route.get("id")),
        prompt_contract_hash=_route_prompt_contract_hash(payload)
        or _string(child_route.get("prompt_contract_hash")),
        route_token_ref=_route_token_ref(payload)
        or _string(child_route.get("route_token_ref")),
        visible_injection_manifest_hash=_route_visible_injection_manifest_hash(payload)
        or _string(child_route.get("visible_injection_manifest_hash")),
    )


def _merge_child_route_prompt_contract(
    primary: Mapping[str, Any],
    fallback: Mapping[str, Any],
) -> dict[str, str]:
    """Fill missing child route identity from another validated payload."""

    fallback_contract = _child_route_prompt_contract_from_payload(fallback)
    return _child_route_prompt_contract(
        route_id=_string(primary.get("route_id"))
        or _string(fallback_contract.get("route_id")),
        route_context_hash=_string(primary.get("route_context_hash"))
        or _string(fallback_contract.get("route_context_hash")),
        prompt_contract_id=_string(primary.get("prompt_contract_id"))
        or _string(fallback_contract.get("prompt_contract_id")),
        prompt_contract_hash=_string(primary.get("prompt_contract_hash"))
        or _string(fallback_contract.get("prompt_contract_hash")),
        route_token_ref=_string(primary.get("route_token_ref"))
        or _string(fallback_contract.get("route_token_ref")),
        visible_injection_manifest_hash=_string(
            primary.get("visible_injection_manifest_hash")
        )
        or _string(fallback_contract.get("visible_injection_manifest_hash")),
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
    route_token_ref: str,
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
    waiver_route_token_ref = _string(waiver.get("route_token_ref"))
    return (
        _string(waiver.get("route_context_hash")) == route_context_hash
        and _string(waiver.get("prompt_contract_id")) == prompt_contract_id
        and (
            not waiver_prompt_hash
            or not prompt_contract_hash
            or waiver_prompt_hash == prompt_contract_hash
        )
        and (
            not route_token_ref
            or waiver_route_token_ref == route_token_ref
        )
    )


def _dispatch_evidence_matches(
    evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
    route_token_ref: str,
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
    evidence_route_token_ref = _string(evidence.get("route_token_ref"))
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
        and (
            not route_token_ref
            or evidence_route_token_ref == route_token_ref
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


def _bounded_startup_evidence_present(
    evidence: Mapping[str, Any],
    *,
    real_startup_events: Any = None,
) -> bool:
    if _explicit_false(evidence.get("bounded")) or _startup_intent_only(evidence):
        return False
    # A host-adapter startup-token surrogate is never close-satisfying real
    # bounded-worker evidence (regression #3104), even when the live startup gate
    # stamps close_satisfying=true — UNLESS a real worker startup event for the
    # same lane lineage joins the surrogate by lineage match.
    if _startup_is_host_adapter_surrogate(evidence):
        if not real_startup_events:
            return False
        join = _startup_real_worker_join(evidence, real_startup_events=real_startup_events)
        if not join["joined"]:
            return False
        # Surrogate joined by real startup — treat as bounded evidence.
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


HOST_ADAPTER_SURROGATE_MATCH_MODE = "host_adapter_startup_token_surrogate"
SESSION_TOKEN_SURROGATE_EVIDENCE_TYPE = "surrogate"
OBSERVER_HOTFIX_EXCEPTION_MODE = "observer_hotfix_exception"
SURROGATE_STARTUP_GATE_SCHEMA_VERSION = "mf_surrogate_startup_evidence_gate.v1"
REAL_WORKER_JOIN_SCHEMA_VERSION = "mf_surrogate_real_worker_join.v1"
CLOSE_TIMELINE_STARTUP_GATE_SCHEMA_VERSION = "mf_close_timeline_startup_gate.v1"

# Lineage fields used to match a surrogate startup against a real worker startup.
_SURROGATE_JOIN_LINEAGE_FIELDS = (
    "task_id",
    "worker_slot_id",
    "runtime_context_id",
    "fence_token",
)


def _startup_is_host_adapter_surrogate(evidence: Mapping[str, Any]) -> bool:
    """True when startup identity came from a host-adapter token surrogate only.

    TOFU mutual-exclusion rule (AC-STARTUP-TOKEN-TOFU-MUTUAL-EXCLUSION-20260610):
    When ``agent_id_match_mode == 'host_adapter_startup_token_surrogate'``, the
    startup MUST be classified as surrogate regardless of
    ``session_token_evidence_type``.  Even if the startup presents a fresh token
    that earns ``'hash'`` (first-sight) or ``'server_verified'`` evidence, the
    host-adapter match mode means the presenter was the HOST, not a verified
    bounded worker.  Token continuity ONLY confers trust when the startup mode is
    ``same_as_allocation_owner`` — where the allocation owner is the direct worker
    executor.

    Rationale for ``server_verified`` on host-adapter mode: the server verified
    that the re-presented token matches the hash committed at first sight, but on
    host-adapter mode the first presenter was also the host.  Hash continuity
    proves continuity with the first-sight host presenter, not with a bounded
    worker.  It should NOT auto-exempt from surrogate classification.

    The live startup gate stamps ``agent_id_match_mode`` /
    ``session_token_evidence_type`` so a surrogate startup is distinguishable
    from a real session-token startup.

    Evidence-type semantics (set by ``_startup_token_evidence`` in
    ``parallel_branch_runtime.py``):
    - ``'server_verified'``: server confirmed hash against allocation-time record.
      NOT a surrogate when ``same_as_allocation_owner``.
      SURROGATE when ``host_adapter_startup_token_surrogate`` (host is presenter).
    - ``'hash'``: first-sight commitment — server recorded the hash.
      NOT a surrogate when ``same_as_allocation_owner``.
      SURROGATE when ``host_adapter_startup_token_surrogate`` (first-sight TOFU
      bypass — closes the QA-#3581 TOFU-HOST-ADAPTER-FIRST-SIGHT finding).
    - ``'claimed_unverified'``: worker presented a token but its hash did NOT
      match the server-recorded allocation hash.  Classified as a SURROGATE
      (token claim is unverified — treat same as no real token).
    - ``'surrogate'``: no session token; surrogate string supplied.  Surrogate.
    - ``''`` (empty): no token.  May be surrogate depending on match_mode.

    Backward compatibility: existing recorded startup events with
    ``agent_id_match_mode != 'host_adapter_startup_token_surrogate'`` and
    ``session_token_evidence_type in ('hash', 'server_verified')`` are NOT
    retroactively classified as surrogates — this change only tightens the
    host-adapter path.
    """

    if not isinstance(evidence, Mapping):
        return False
    token_type = _string(evidence.get("session_token_evidence_type")).lower()
    # "surrogate" evidence type is always a surrogate — no real token present.
    if token_type == SESSION_TOKEN_SURROGATE_EVIDENCE_TYPE:
        return True
    # "claimed_unverified": worker presented a token but the server found a hash
    # mismatch — the claim is not trustworthy; treat as surrogate.
    if token_type == "claimed_unverified":
        return True
    # Resolve match_mode early — needed for TOFU mutual-exclusion check below.
    match_mode = _string(
        evidence.get("agent_id_match_mode")
        or _nested_mapping(evidence, "identity_join").get("agent_id_match_mode")
    ).lower()
    # TOFU mutual-exclusion gate (AC-STARTUP-TOKEN-TOFU-MUTUAL-EXCLUSION-20260610):
    # host_adapter_startup_token_surrogate mode is ALWAYS a surrogate regardless of
    # session_token_evidence_type.  A first-sight ('hash') or re-presentation
    # ('server_verified') token on host-adapter mode earns no trust because the
    # presenter is the host, not a verified bounded worker.
    if match_mode == HOST_ADAPTER_SURROGATE_MATCH_MODE:
        return True
    agent_id = _string(evidence.get("agent_id"))
    allocation_owner = _string(
        evidence.get("allocation_owner")
        or evidence.get("observer_allocation_owner")
        or evidence.get("expected_agent_id")
    )
    registered_host_adapter = bool(
        _bool(evidence.get("registered_host_adapter_spawn_present"))
        or _nested_mapping(evidence, "registered_host_adapter_spawn")
        or _nested_mapping(evidence, "host_adapter_spawn_identity")
        or _nested_mapping(evidence, "host_adapter_startup_identity")
    )
    if (
        agent_id
        and allocation_owner
        and agent_id != allocation_owner
        and match_mode != "same_as_allocation_owner"
        and not registered_host_adapter
    ):
        return True
    # Server-verified or first-sight hash on same_as_allocation_owner (or other
    # non-host-adapter) mode: NOT a surrogate.
    if token_type in ("server_verified", "hash"):
        if _string(evidence.get("session_token_hash")) and _bool(
            evidence.get("session_token_present")
        ):
            return False
    # Legacy path: real session token hash present without explicit evidence type
    # (e.g. startup events recorded before this fix) — NOT a surrogate.
    if _string(evidence.get("session_token_hash")) and _bool(
        evidence.get("session_token_present")
    ):
        return False
    return bool(
        _bool(evidence.get("host_adapter_startup_token_accepted"))
        and not _string(evidence.get("session_token_hash"))
        and not _bool(evidence.get("session_token_present"))
    )


def _startup_real_worker_join(
    surrogate_evidence: Mapping[str, Any],
    *,
    real_startup_events: Any = None,
) -> dict[str, Any]:
    """Check whether a surrogate startup can be joined to a real worker startup.

    When the HOST (observer adapter) recorded a surrogate startup, a later REAL
    parallel_branch_startup from the actual worker with matching lineage
    (task_id / worker_slot_id / runtime_context_id / fence_token) upgrades the
    surrogate to close-satisfying evidence.  The join is evidence-based: it
    requires the real startup to carry matching lineage fields, NOT merely the
    presence of the surrogate flag.

    Returns a structured result:
    - ``joined``: True when a real startup event satisfies the join.
    - ``join_event_id``: the event id of the real startup that satisfied it
      (empty string when not joined).
    - ``reason``: human-readable reason for pass or refusal.
    - ``lineage``: the lineage fields used for matching.
    """

    surrogate = dict(surrogate_evidence) if isinstance(surrogate_evidence, Mapping) else {}
    # Extract lineage from surrogate.
    surrogate_task_id = _string(surrogate.get("task_id"))
    surrogate_worker_slot_id = _string(
        surrogate.get("worker_slot_id") or surrogate.get("worker_id")
    )
    surrogate_runtime_context_id = _string(surrogate.get("runtime_context_id"))
    surrogate_fence_token = _string(surrogate.get("fence_token"))

    lineage = {
        "task_id": surrogate_task_id,
        "worker_slot_id": surrogate_worker_slot_id,
        "runtime_context_id": surrogate_runtime_context_id,
        "fence_token": surrogate_fence_token,
    }

    if not real_startup_events:
        return {
            "schema_version": REAL_WORKER_JOIN_SCHEMA_VERSION,
            "joined": False,
            "join_event_id": "",
            "reason": "surrogate_only_no_real_startup_events_provided",
            "lineage": lineage,
        }

    candidates: list[Mapping[str, Any]] = []
    if isinstance(real_startup_events, Mapping):
        candidates = [real_startup_events]
    elif isinstance(real_startup_events, Sequence) and not isinstance(
        real_startup_events, (str, bytes, bytearray)
    ):
        for item in real_startup_events:
            if isinstance(item, Mapping):
                candidates.append(item)

    for event in candidates:
        # Accept both raw startup gate dicts and timeline event wrappers.
        gate: Mapping[str, Any] = event
        payload = _nested_mapping(event, "payload")
        if payload:
            nested_gate = _nested_mapping(payload, "mf_subagent_startup_gate")
            if nested_gate:
                gate = nested_gate
        # A real startup must NOT itself be a surrogate.
        if _startup_is_host_adapter_surrogate(gate):
            continue
        match_mode = _string(
            gate.get("agent_id_match_mode")
            or _nested_mapping(gate, "identity_join").get("agent_id_match_mode")
        ).lower()
        if match_mode != "same_as_allocation_owner":
            continue
        # It must carry a real session token.
        if not _string(gate.get("session_token_hash")) and not _bool(
            gate.get("session_token_present")
        ):
            token_type = _string(gate.get("session_token_evidence_type")).lower()
            if token_type not in ("hash",):
                continue
        # F3 fix: all four lineage fields must be NON-EMPTY on the candidate
        # AND equal to the surrogate's lineage.  An empty candidate field means
        # the event does not carry lineage and must NOT join any surrogate.
        if surrogate_task_id:
            candidate_task = _string(gate.get("task_id"))
            if not candidate_task or candidate_task != surrogate_task_id:
                continue
        if surrogate_worker_slot_id:
            candidate_slot = _string(
                gate.get("worker_slot_id") or gate.get("worker_id")
            )
            if not candidate_slot or candidate_slot != surrogate_worker_slot_id:
                continue
        if surrogate_runtime_context_id:
            candidate_rctx = _string(gate.get("runtime_context_id"))
            if not candidate_rctx or candidate_rctx != surrogate_runtime_context_id:
                continue
        if surrogate_fence_token:
            candidate_fence = _string(gate.get("fence_token"))
            if not candidate_fence or candidate_fence != surrogate_fence_token:
                continue
        # Matched — extract event id.
        join_event_id = _string(
            event.get("id")
            or event.get("event_id")
            or event.get("timeline_event_id")
            or gate.get("startup_event_id")
            or gate.get("event_id")
        )
        return {
            "schema_version": REAL_WORKER_JOIN_SCHEMA_VERSION,
            "joined": True,
            "join_event_id": join_event_id,
            "reason": "real_worker_startup_lineage_match",
            "matched_startup_gate": dict(gate),
            "matched_lineage_fields": [
                f for f in _SURROGATE_JOIN_LINEAGE_FIELDS
                if lineage.get(f)
            ],
            "lineage": lineage,
        }

    return {
        "schema_version": REAL_WORKER_JOIN_SCHEMA_VERSION,
        "joined": False,
        "join_event_id": "",
        "reason": "surrogate_only_no_matching_real_startup_in_events",
        "lineage": lineage,
    }


def _observer_hotfix_exception_present(events: Any) -> bool:
    """Detect an explicit observer_hotfix_exception mode event in the evidence."""

    candidates: list[Mapping[str, Any]] = []
    if isinstance(events, Mapping):
        candidates = [events]
    elif isinstance(events, Sequence) and not isinstance(events, (str, bytes)):
        candidates = [item for item in events if isinstance(item, Mapping)]
    for event in candidates:
        marker = " ".join(
            _string(event.get(key))
            for key in ("event_kind", "event_type", "phase", "work_mode", "mode")
        ).lower().replace("-", "_").replace(".", "_")
        if OBSERVER_HOTFIX_EXCEPTION_MODE in marker:
            status = _string(event.get("status") or event.get("decision")).lower()
            if status not in _FAIL_STATUSES:
                return True
        for nested_key in ("payload", "verification"):
            nested = _nested_mapping(event, nested_key)
            nested_mode = _string(
                nested.get("work_mode") or nested.get("mode")
            ).lower().replace("-", "_").replace(" ", "_")
            if nested_mode == OBSERVER_HOTFIX_EXCEPTION_MODE:
                return True
    return False


def surrogate_startup_evidence_gate(
    startup_evidence: Mapping[str, Any] | None,
    *,
    events: Any = None,
    real_startup_events: Any = None,
) -> dict[str, Any]:
    """Demote host-adapter surrogate startup evidence. [regression #3104]

    When the startup token/identity is a host_adapter_startup_token_surrogate
    (``session_token_evidence_type == "surrogate"``), the startup must NOT be
    close-satisfying for a real bounded-worker or independent-QA evidence
    requirement UNLESS:

    1. An explicit observer_hotfix_exception mode event is present — but even
       then it is NOT counted as close-satisfying real-worker evidence. A real
       session-token startup stays close-satisfying.
    2. [AC fix] A real worker startup event for the same lane lineage
       (task_id / worker_slot_id / runtime_context_id / fence_token) exists in
       ``real_startup_events``.  When a real startup joins the surrogate by
       lineage match, the surrogate becomes close-satisfying and the join event
       id is surfaced in the response.  The join is evidence-based — it requires
       a matching real startup event, NOT a flag that bypasses the gate.

    When neither condition is met and the startup is a true surrogate, the
    refusal remains.  Surrogate-only lanes stay blocked.
    """

    evidence = startup_evidence if isinstance(startup_evidence, Mapping) else {}
    is_surrogate = _startup_is_host_adapter_surrogate(evidence)
    hotfix_exception = _observer_hotfix_exception_present(events)
    declared_close_satisfying = not _explicit_false(evidence.get("close_satisfying"))
    join_result: dict[str, Any] = {
        "schema_version": REAL_WORKER_JOIN_SCHEMA_VERSION,
        "joined": False,
        "join_event_id": "",
        "reason": "not_applicable_not_a_surrogate",
        "lineage": {},
    }
    if not is_surrogate:
        close_satisfying = declared_close_satisfying
        reason = "real_session_token_startup"
    else:
        # Check whether a real worker startup joins the surrogate by lineage.
        if real_startup_events:
            join_result = _startup_real_worker_join(
                evidence, real_startup_events=real_startup_events
            )
        else:
            join_result = {
                "schema_version": REAL_WORKER_JOIN_SCHEMA_VERSION,
                "joined": False,
                "join_event_id": "",
                "reason": "surrogate_only_no_real_startup_events_provided",
                "lineage": {
                    field: _string(evidence.get(field))
                    for field in _SURROGATE_JOIN_LINEAGE_FIELDS
                },
            }
        if join_result["joined"]:
            # A real worker startup with matching lineage upgrades the surrogate.
            close_satisfying = True
            reason = (
                "surrogate_joined_by_real_worker_startup:"
                + _string(join_result.get("join_event_id"))
            )
        else:
            # Surrogate startup is never close-satisfying real-worker evidence
            # unless joined by a real startup.  The hotfix exception only lets
            # the startup proceed under a relaxed identity match — it does NOT
            # promote surrogate evidence to real-worker close evidence.
            close_satisfying = False
            reason = (
                "host_adapter_surrogate_under_observer_hotfix_exception_not_real_worker_evidence"
                if hotfix_exception
                else "host_adapter_surrogate_blocked_without_observer_hotfix_exception"
            )
    return {
        "schema_version": SURROGATE_STARTUP_GATE_SCHEMA_VERSION,
        "is_host_adapter_surrogate": is_surrogate,
        "observer_hotfix_exception_present": hotfix_exception,
        "declared_close_satisfying": declared_close_satisfying,
        "close_satisfying": close_satisfying,
        "counts_as_real_worker_evidence": close_satisfying,
        "counts_as_independent_qa_evidence": close_satisfying,
        "status": "close_satisfying" if close_satisfying else "demoted",
        "reason": reason,
        "real_worker_join": join_result,
    }


def _timeline_startup_gate_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    for container in (
        _nested_mapping(event, "payload"),
        _nested_mapping(event, "verification"),
        _nested_mapping(event, "artifact_refs"),
        event,
    ):
        if not container:
            continue
        finish_gate = _nested_mapping(container, "mf_subagent_finish_gate")
        if finish_gate:
            startup_projection = _finish_gate_startup_close_projection(
                event,
                finish_gate,
            )
            if startup_projection:
                return startup_projection
        for key in (
            "mf_subagent_startup_gate",
            "startup_evidence",
            "bounded_startup_evidence",
        ):
            gate = _nested_mapping(container, key)
            if gate:
                return gate
    if (
        _string(event.get("schema_version")) == "mf_subagent_startup_gate.v1"
        or _string(event.get("gate_kind") or event.get("kind")).lower()
        == "mf_subagent.startup"
    ):
        return dict(event)
    return {}


def _finish_gate_startup_close_projection(
    event: Mapping[str, Any],
    finish_gate: Mapping[str, Any],
) -> dict[str, Any]:
    status = _string(event.get("status") or event.get("decision")).lower()
    if status and status not in (*_PASS_STATUSES, "accepted", "approved", "allowed", "allow"):
        return {}
    startup_identity_gate = _nested_mapping(finish_gate, "startup_worker_identity_gate")
    worker_attestation_gate = _nested_mapping(finish_gate, "worker_self_attestation_gate")
    if not (_bool(startup_identity_gate.get("passed")) and _bool(worker_attestation_gate.get("passed"))):
        return {}
    startup_evidence = dict(_nested_mapping(finish_gate, "startup_evidence"))
    if not startup_evidence:
        return {}
    attestation = _nested_mapping(worker_attestation_gate, "attestation")
    if not attestation:
        attestation = _nested_mapping(finish_gate, "worker_self_attestation")
    worker_session_id = _string(
        worker_attestation_gate.get("worker_session_id")
        or startup_identity_gate.get("worker_session_id")
        or attestation.get("worker_session_id")
        or startup_evidence.get("worker_session_id")
    )
    worker_transcript_path = _string(
        worker_attestation_gate.get("worker_transcript_path")
        or startup_identity_gate.get("worker_transcript_path")
        or attestation.get("worker_transcript_path")
        or startup_evidence.get("worker_transcript_path")
    )
    worker_transcript_ref = _string(
        worker_attestation_gate.get("worker_transcript_ref")
        or startup_identity_gate.get("worker_transcript_ref")
        or attestation.get("worker_transcript_ref")
        or startup_evidence.get("worker_transcript_ref")
    )
    harness_type = _string(
        worker_attestation_gate.get("harness_type")
        or startup_identity_gate.get("harness_type")
        or attestation.get("harness_type")
        or startup_evidence.get("harness_type")
    )
    filer_principal = _string(
        attestation.get("filer_principal")
        or worker_attestation_gate.get("filer_principal")
        or startup_evidence.get("filer_principal")
        or startup_evidence.get("actor")
    )
    startup_evidence.update(
        {
            "schema_version": "mf_subagent_startup_gate.v1",
            "gate_kind": "mf_subagent.startup",
            "close_satisfying": True,
            "worker_self_attestation": dict(attestation),
            "worker_session_id": worker_session_id,
            "worker_transcript_path": worker_transcript_path,
            "worker_transcript_ref": worker_transcript_ref,
            "harness_type": harness_type,
            "filer_principal": filer_principal,
            "finish_gate_startup_projection": True,
            "finish_gate_event_id": _string(event.get("id") or event.get("event_id")),
        }
    )
    return startup_evidence


def close_timeline_startup_event_gate(events: Any) -> dict[str, Any]:
    """Evaluate close-timeline startup events for close-satisfying evidence.

    The shared timeline verifier works from event categories.  This gate is the
    close-path adapter that removes startup events that are not actual,
    self-attesting worker evidence.  Surrogate startup events only count when
    joined by a real same_as_allocation_owner startup for the same lane lineage;
    same-owner startups still need passed worker transcript attestation.
    """

    if isinstance(events, Mapping):
        rows: list[Mapping[str, Any]] = [events]
    elif isinstance(events, Sequence) and not isinstance(
        events, (str, bytes, bytearray)
    ):
        rows = [item for item in events if isinstance(item, Mapping)]
    else:
        rows = []
    passing_rows = [
        row
        for row in rows
        if _string(row.get("status") or row.get("decision")).lower()
        in (*_PASS_STATUSES, "accepted", "approved", "allowed", "allow")
    ]
    demoted: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    startup_event_count = 0
    for index, event in enumerate(rows):
        gate = _timeline_startup_gate_from_event(event)
        if not gate:
            continue
        startup_event_count += 1
        surrogate_gate = surrogate_startup_evidence_gate(
            gate,
            real_startup_events=passing_rows,
        )
        close_satisfying_startup = _close_satisfying_startup_evidence(
            gate,
            surrogate_join_gate=surrogate_gate,
        )
        worker_self_attestation_gate = _worker_self_attestation_close_gate(
            close_satisfying_startup
        )
        event_ref = {
            "index": index,
            "id": _string(event.get("id") or event.get("event_id")),
            "event_kind": _string(event.get("event_kind")),
            "phase": _string(event.get("phase")),
            "status": _string(event.get("status") or event.get("decision")),
            "reason": surrogate_gate.get("reason", ""),
            "real_worker_join": surrogate_gate.get("real_worker_join", {}),
            "worker_self_attestation_gate": worker_self_attestation_gate,
        }
        if surrogate_gate.get("close_satisfying") and worker_self_attestation_gate.get("passed"):
            accepted.append(event_ref)
        else:
            if not worker_self_attestation_gate.get("passed"):
                event_ref["reason"] = "worker_self_attestation_not_close_satisfying"
                event_ref["worker_self_attestation_blockers"] = (
                    worker_self_attestation_gate.get("blockers") or []
                )
            demoted.append(event_ref)
    passed = bool(accepted) if startup_event_count else True
    return {
        "schema_version": CLOSE_TIMELINE_STARTUP_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "accepted_startup_events": accepted,
        "demoted_startup_events": demoted,
        "demoted_startup_event_indexes": [item["index"] for item in demoted],
    }


def close_timeline_events_for_verification(events: Any) -> dict[str, Any]:
    gate = close_timeline_startup_event_gate(events)
    if isinstance(events, Mapping):
        rows: list[Mapping[str, Any]] = [events]
    elif isinstance(events, Sequence) and not isinstance(
        events, (str, bytes, bytearray)
    ):
        rows = [item for item in events if isinstance(item, Mapping)]
    else:
        rows = []
    demoted_indexes = set(gate.get("demoted_startup_event_indexes") or [])
    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(rows):
        row = dict(event)
        if index in demoted_indexes:
            row["status"] = "demoted"
            verification = dict(_mapping(row.get("verification"), field_name="verification"))
            verification["mf_close_timeline_startup_gate"] = gate
            row["verification"] = verification
        normalized.append(row)
    return {
        "schema_version": "mf_close_timeline_events_for_verification.v1",
        "events": normalized,
        "startup_gate": gate,
    }


def _startup_evidence_matches(
    evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
    route_token_ref: str,
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
    evidence_route_token_ref = _string(evidence.get("route_token_ref"))
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
        and (
            not route_token_ref
            or evidence_route_token_ref == route_token_ref
        )
    )


def _trusted_startup_event_rows(events: Any) -> list[Mapping[str, Any]]:
    if isinstance(events, Mapping):
        return [events]
    if isinstance(events, Sequence) and not isinstance(
        events, (str, bytes, bytearray)
    ):
        return [item for item in events if isinstance(item, Mapping)]
    return []


def _startup_finish_payload_lineage_gate(
    startup_evidence: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    mismatches: list[dict[str, str]] = []
    checked = False
    for field, aliases in (
        ("task_id", ("task_id",)),
        ("parent_task_id", ("parent_task_id",)),
        ("runtime_context_id", ("runtime_context_id",)),
        ("fence_token", ("fence_token",)),
    ):
        expected = _dispatch_string(payload, names=aliases, nested_keys=(("evidence", aliases),))
        actual = _string(startup_evidence.get(field))
        if expected:
            checked = True
        if expected and actual and actual != expected:
            mismatches.append({"field": field, "expected": expected, "actual": actual})
    return {
        "checked": checked,
        "passed": not mismatches,
        "mismatches": mismatches,
    }


def _route_startup_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve startup evidence only from server/DB-derived startup events."""

    rows = _trusted_startup_event_rows(
        payload.get("real_startup_events")
        or payload.get("db_startup_events")
        or payload.get("server_startup_events")
    )
    candidates: list[dict[str, Any]] = []
    for event in rows:
        event_kind = _string(event.get("event_kind") or event.get("event_type"))
        if event_kind and "mf_subagent_startup" not in event_kind:
            continue
        gate = _timeline_startup_gate_from_event(event)
        if gate:
            candidate = dict(gate)
            source_event_id = _string(
                event.get("id")
                or event.get("event_id")
                or event.get("timeline_event_id")
                or candidate.get("startup_event_id")
                or candidate.get("event_id")
                or candidate.get("id")
            )
            if source_event_id and not _string(candidate.get("startup_event_id")):
                candidate["startup_event_id"] = source_event_id
            source_event_kind = _string(event.get("event_kind") or event.get("event_type"))
            if source_event_kind and not _string(candidate.get("startup_event_kind")):
                candidate["startup_event_kind"] = source_event_kind
            source_event_status = _string(event.get("status") or event.get("decision"))
            if source_event_status and not _string(candidate.get("startup_event_status")):
                candidate["startup_event_status"] = source_event_status
            event_actor = _string(event.get("actor"))
            if event_actor and not _string(candidate.get("actor")):
                candidate["actor"] = event_actor
            candidates.append(candidate)
    if not candidates:
        return {}
    lineage_candidates: list[dict[str, Any]] = []
    mismatched_candidates: list[dict[str, Any]] = []
    lineage_checked = False
    for candidate in candidates:
        lineage_gate = _startup_finish_payload_lineage_gate(candidate, payload)
        if lineage_gate.get("checked"):
            lineage_checked = True
        if lineage_gate.get("passed"):
            lineage_candidates.append(candidate)
            continue
        mismatched = dict(candidate)
        mismatched["finish_lineage_mismatches"] = list(
            lineage_gate.get("mismatches") or []
        )
        mismatched_candidates.append(mismatched)
    selectable = lineage_candidates or ([] if lineage_checked else candidates)
    if not selectable and mismatched_candidates:
        return mismatched_candidates[0]
    for candidate in selectable:
        if _startup_worker_identity_close_gate(candidate).get("passed"):
            return candidate
    for candidate in selectable:
        if _worker_self_attestation_close_gate(candidate).get("passed"):
            return candidate
    return selectable[0] if selectable else {}
    return {}


def _route_action_startup_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve startup evidence for the pre-mutation route-action gate."""

    server_evidence = _route_startup_evidence(payload)
    if server_evidence:
        return server_evidence
    return _first_mapping(
        payload,
        (
            "mf_subagent_startup_gate",
            "startup_evidence",
            "bounded_startup_evidence",
        ),
    )


def _close_satisfying_startup_evidence(
    startup_evidence: Mapping[str, Any],
    *,
    surrogate_join_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the real startup gate that close evidence actually depends on."""

    evidence = dict(startup_evidence) if isinstance(startup_evidence, Mapping) else {}
    if not _startup_is_host_adapter_surrogate(evidence):
        return evidence
    real_worker_join = _nested_mapping(surrogate_join_gate, "real_worker_join")
    if not _bool(real_worker_join.get("joined")):
        return evidence
    matched_gate = real_worker_join.get("matched_startup_gate")
    if isinstance(matched_gate, Mapping):
        return dict(matched_gate)
    return evidence


def _worker_self_attestation_close_gate(
    startup_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    attestation = _nested_mapping(startup_evidence, "worker_self_attestation")
    worker_session_id = _string(
        startup_evidence.get("worker_session_id")
        or attestation.get("worker_session_id")
    )
    worker_transcript_path = _string(
        startup_evidence.get("worker_transcript_path")
        or attestation.get("worker_transcript_path")
    )
    worker_transcript_ref = _string(
        startup_evidence.get("worker_transcript_ref")
        or attestation.get("worker_transcript_ref")
    )
    harness_type = _string(
        startup_evidence.get("harness_type") or attestation.get("harness_type")
    )
    blockers: list[str] = []
    if _startup_is_host_adapter_surrogate(startup_evidence):
        blockers.append("startup_is_host_adapter_surrogate")
    filer_principal = _string(
        startup_evidence.get("filer_principal") or startup_evidence.get("actor")
    )
    if not filer_principal:
        blockers.append("missing_filer_principal")
    elif worker_session_id and filer_principal != worker_session_id:
        blockers.append("startup_filer_principal_not_worker_session")
    if filer_principal in {"mf_sub", "observer"}:
        blockers.append("startup_filer_is_generic_or_observer")
    if _string(startup_evidence.get("filed_on_behalf_by")) or _bool(
        startup_evidence.get("filed_on_behalf")
    ):
        blockers.append("startup_filed_on_behalf")
    known_bad = (
        _bool(startup_evidence.get("known_bad_playback_4178"))
        or _bool(attestation.get("known_bad_playback_4178"))
        or "4178" in _string(startup_evidence.get("agent_id")).lower()
        or "4178" in _string(startup_evidence.get("startup_source")).lower()
    )
    if known_bad:
        blockers.append("known_bad_playback_4178_shape")
    if not attestation:
        blockers.append("missing_worker_self_attestation")
    else:
        status = _string(attestation.get("status")).lower()
        self_attesting = _bool(
            attestation.get("worker_self_attesting")
            or attestation.get("self_attesting")
            or startup_evidence.get("worker_self_attesting")
            or startup_evidence.get("self_attesting")
        )
        if status not in _PASS_STATUSES or not self_attesting:
            blockers.append("worker_self_attestation_not_passed")
    if not worker_session_id:
        blockers.append("missing_worker_session_id")
    if not (worker_transcript_path or worker_transcript_ref):
        blockers.append("missing_worker_transcript_ref_or_path")
    if harness_type not in {"claude", "codex"}:
        blockers.append("missing_or_unsupported_harness_type")
    passed = not blockers
    return {
        "schema_version": "mf_subagent_worker_self_attestation_close_gate.v1",
        "status": "passed" if passed else "blocked",
        "passed": passed,
        "worker_self_attesting": passed,
        "worker_session_id": worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "harness_type": harness_type,
        "blockers": blockers,
        "attestation": dict(attestation),
    }


def _attestation_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_string(item) for item in value if _string(item)]
    return [_string(value)] if _string(value) else []


def _finish_worker_attestation_candidate(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    evidence = _nested_mapping(payload, "evidence")
    for container_name, container in (
        ("payload", payload),
        ("evidence", evidence),
    ):
        if not isinstance(container, Mapping):
            continue
        for key in _FINISH_WORKER_ATTESTATION_KEYS:
            candidate = container.get(key)
            if isinstance(candidate, Mapping):
                return dict(candidate), f"{container_name}.{key}"
    return {}, ""


def _public_finish_worker_attestation(
    attestation: Mapping[str, Any],
    *,
    worker_session_id: str = "",
    filer_principal: str = "",
    worker_transcript_path: str = "",
    worker_transcript_ref: str = "",
    harness_type: str = "",
) -> dict[str, Any]:
    safe: dict[str, Any] = {
        key: attestation.get(key)
        for key in _FINISH_WORKER_ATTESTATION_PUBLIC_FIELDS
        if key in attestation
    }
    if worker_session_id and not _string(safe.get("worker_session_id")):
        safe["worker_session_id"] = worker_session_id
    if filer_principal and not _string(safe.get("filer_principal")):
        safe["filer_principal"] = filer_principal
    if worker_transcript_path and not _string(safe.get("worker_transcript_path")):
        safe["worker_transcript_path"] = worker_transcript_path
    if worker_transcript_ref and not _string(safe.get("worker_transcript_ref")):
        safe["worker_transcript_ref"] = worker_transcript_ref
    if harness_type and not _string(safe.get("harness_type")):
        safe["harness_type"] = harness_type
    for list_key in ("blockers", "finish_time_blockers"):
        if list_key in safe:
            safe[list_key] = _attestation_string_list(safe.get(list_key))
    return safe


def _finish_attestation_value(
    name: str,
    *,
    attestation: Mapping[str, Any],
    payload: Mapping[str, Any],
    evidence: Mapping[str, Any],
    aliases: Sequence[str] = (),
) -> Any:
    for key in (name, *aliases):
        if key in attestation:
            return attestation.get(key)
    for key in (name, *aliases):
        if key in payload:
            return payload.get(key)
    for key in (name, *aliases):
        if key in evidence:
            return evidence.get(key)
    return None


def _normalized_worker_filer(value: Any) -> str:
    return "_".join(_string(value).lower().replace("-", "_").split())


def _normalized_worker_harness_type(value: Any) -> str:
    normalized = _normalized_worker_filer(value)
    return _WORKER_HARNESS_TYPE_ALIASES.get(normalized, normalized)


def _finish_time_worker_attestation_gate(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate finish-time worker self-attestation from the finish payload."""

    attestation, source = _finish_worker_attestation_candidate(payload)
    evidence = _nested_mapping(payload, "evidence")
    worker_session_id = _string(
        _finish_attestation_value(
            "worker_session_id",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("session_id",),
        )
    )
    worker_transcript_path = _string(
        _finish_attestation_value(
            "worker_transcript_path",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("transcript_path",),
        )
    )
    worker_transcript_ref = _string(
        _finish_attestation_value(
            "worker_transcript_ref",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("transcript_ref", "worker_transcript_uri", "transcript_uri"),
        )
    )
    harness_type = _normalized_worker_harness_type(
        _finish_attestation_value(
            "harness_type",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("worker_harness_type",),
        )
    )
    attestation_phase = _string(
        _finish_attestation_value(
            "attestation_phase",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("worker_attestation_phase",),
        )
    ).lower()
    status = _string(
        _finish_attestation_value(
            "status",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
        )
    ).lower()
    worker_self_attesting = _bool(
        _finish_attestation_value(
            "worker_self_attesting",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("self_attesting",),
        )
    )
    finish_time_self_attesting = _bool(
        _finish_attestation_value(
            "finish_time_self_attesting",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
        )
    )
    finish_time_blockers = _attestation_string_list(
        _finish_attestation_value(
            "finish_time_blockers",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
        )
    )
    filer_principal = _string(
        _finish_attestation_value(
            "filer_principal",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("actor", "filed_by"),
        )
    )
    filed_on_behalf_by = _string(
        _finish_attestation_value(
            "filed_on_behalf_by",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("on_behalf_of",),
        )
    )
    filed_on_behalf = _bool(
        _finish_attestation_value(
            "filed_on_behalf",
            attestation=attestation,
            payload=payload,
            evidence=evidence,
            aliases=("on_behalf",),
        )
    )
    blockers: list[str] = []
    if not attestation:
        blockers.append("missing_finish_time_worker_self_attestation")
    if attestation_phase == "startup":
        blockers.append("attestation_phase_startup_not_finish")
    if status not in _PASS_STATUSES or not worker_self_attesting:
        blockers.append("worker_self_attestation_not_passed")
    if not finish_time_self_attesting:
        blockers.append("finish_time_self_attestation_not_passed")
    if finish_time_blockers:
        blockers.append("finish_time_blockers_present")
    if not worker_session_id:
        blockers.append("missing_worker_session_id")
    if not filer_principal:
        blockers.append("missing_filer_principal")
    elif worker_session_id and filer_principal != worker_session_id:
        blockers.append("finish_attestation_filer_principal_not_worker_session")
    if not (worker_transcript_path or worker_transcript_ref):
        blockers.append("missing_worker_transcript_ref_or_path")
    if harness_type not in _SUPPORTED_WORKER_HARNESS_TYPES:
        blockers.append("missing_or_unsupported_harness_type")
    normalized_filer = _normalized_worker_filer(filer_principal)
    if normalized_filer in _GENERIC_OR_OBSERVER_WORKER_FILERS:
        blockers.append("finish_attestation_filer_is_generic_or_observer")
    if filed_on_behalf or filed_on_behalf_by:
        blockers.append("finish_attestation_filed_on_behalf")
    passed = not blockers
    public_attestation = _public_finish_worker_attestation(
        attestation,
        worker_session_id=worker_session_id,
        filer_principal=filer_principal,
        worker_transcript_path=worker_transcript_path,
        worker_transcript_ref=worker_transcript_ref,
        harness_type=harness_type,
    )
    if finish_time_self_attesting and "finish_time_self_attesting" not in public_attestation:
        public_attestation["finish_time_self_attesting"] = True
    if "finish_time_blockers" not in public_attestation:
        public_attestation["finish_time_blockers"] = finish_time_blockers
    if status and "status" not in public_attestation:
        public_attestation["status"] = status
    if worker_self_attesting and "worker_self_attesting" not in public_attestation:
        public_attestation["worker_self_attesting"] = True
    if attestation_phase and "attestation_phase" not in public_attestation:
        public_attestation["attestation_phase"] = attestation_phase
    return {
        "schema_version": "mf_subagent_finish_time_worker_self_attestation_gate.v1",
        "status": "passed" if passed else "blocked",
        "passed": passed,
        "close_satisfying": passed,
        "source": source,
        "attestation_phase": attestation_phase or "",
        "worker_self_attesting": worker_self_attesting,
        "finish_time_self_attesting": finish_time_self_attesting,
        "worker_session_id": worker_session_id,
        "filer_principal": filer_principal,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "harness_type": harness_type,
        "finish_time_blockers": finish_time_blockers,
        "blockers": blockers,
        "attestation": public_attestation,
    }


def _startup_worker_identity_close_gate(
    startup_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate that startup evidence is a real worker identity, not finish proof."""

    evidence = dict(startup_evidence) if isinstance(startup_evidence, Mapping) else {}
    attestation = _nested_mapping(evidence, "worker_self_attestation")
    worker_session_id = _string(
        evidence.get("worker_session_id") or attestation.get("worker_session_id")
    )
    worker_transcript_path = _string(
        evidence.get("worker_transcript_path")
        or attestation.get("worker_transcript_path")
    )
    worker_transcript_ref = _string(
        evidence.get("worker_transcript_ref")
        or evidence.get("transcript_ref")
        or attestation.get("worker_transcript_ref")
        or attestation.get("transcript_ref")
    )
    harness_type = _normalized_worker_harness_type(
        evidence.get("harness_type") or attestation.get("harness_type")
    )
    filer_principal = _string(evidence.get("filer_principal") or evidence.get("actor"))
    read_receipt = _nested_mapping(evidence, "read_receipt")
    read_receipt_filer = _string(
        evidence.get("read_receipt_filer_principal")
        or evidence.get("read_receipt_actor")
        or read_receipt.get("filer_principal")
        or read_receipt.get("actor")
        or read_receipt.get("filed_by")
    )
    blockers: list[str] = []
    status = _string(evidence.get("status") or evidence.get("decision")).lower()
    if status in _FAIL_STATUSES or _explicit_false(evidence.get("ok")):
        blockers.append("startup_gate_not_passed")
    if _startup_is_host_adapter_surrogate(evidence):
        blockers.append("startup_is_host_adapter_surrogate")
    if evidence.get("finish_lineage_mismatches"):
        blockers.append("startup_finish_lineage_mismatch")
    if not filer_principal:
        blockers.append("missing_filer_principal")
    elif worker_session_id and filer_principal != worker_session_id:
        blockers.append("startup_filer_principal_not_worker_session")
    if _normalized_worker_filer(filer_principal) in _GENERIC_OR_OBSERVER_WORKER_FILERS:
        blockers.append("startup_filer_is_generic_or_observer")
    if read_receipt_filer:
        normalized_receipt_filer = _normalized_worker_filer(read_receipt_filer)
        if normalized_receipt_filer in _GENERIC_OR_OBSERVER_WORKER_FILERS:
            blockers.append("read_receipt_filer_is_generic_or_observer")
        elif worker_session_id and read_receipt_filer != worker_session_id:
            blockers.append("read_receipt_filer_principal_not_worker_session")
    if _string(evidence.get("filed_on_behalf_by")) or _bool(
        evidence.get("filed_on_behalf")
    ):
        blockers.append("startup_filed_on_behalf")
    known_bad = (
        _bool(evidence.get("known_bad_playback_4178"))
        or _bool(attestation.get("known_bad_playback_4178"))
        or "4178" in _string(evidence.get("agent_id")).lower()
        or "4178" in _string(evidence.get("startup_source")).lower()
    )
    if known_bad:
        blockers.append("known_bad_playback_4178_shape")
    if not worker_session_id:
        blockers.append("missing_worker_session_id")
    if not (worker_transcript_path or worker_transcript_ref):
        blockers.append("missing_worker_transcript_ref_or_path")
    if harness_type not in _SUPPORTED_WORKER_HARNESS_TYPES:
        blockers.append("missing_or_unsupported_harness_type")
    if not _string(evidence.get("runtime_context_id")):
        blockers.append("missing_runtime_context_id")
    if not _fence_evidence_present(evidence):
        blockers.append("missing_fence_token")
    if not _string(evidence.get("route_id")):
        blockers.append("missing_route_id")
    for route_field in (
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
    ):
        if not _string(evidence.get(route_field)):
            blockers.append(f"missing_{route_field}")
    passed = not blockers
    return {
        "schema_version": "mf_subagent_startup_worker_identity_close_gate.v1",
        "status": "passed" if passed else "blocked",
        "passed": passed,
        "real_startup_identity": passed,
        "worker_session_id": worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "harness_type": harness_type,
        "runtime_context_id": _string(evidence.get("runtime_context_id")),
        "read_receipt_filer_principal": read_receipt_filer,
        "blockers": blockers,
    }


def _bounded_worker_evidence_matches(
    dispatch_evidence: Mapping[str, Any],
    startup_evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
    route_token_ref: str,
) -> dict[str, Any]:
    dispatch_matches = _dispatch_evidence_matches(
        dispatch_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
        route_token_ref=route_token_ref,
    )
    startup_matches = _startup_evidence_matches(
        startup_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
        route_token_ref=route_token_ref,
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
    route_token_ref = _route_token_ref(payload)
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
        route_token_ref=route_token_ref,
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
    startup_evidence = _route_action_startup_evidence(payload)
    bounded_worker_evidence = _bounded_worker_evidence_matches(
        dispatch_evidence,
        startup_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
        route_token_ref=route_token_ref,
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
        "route_token_ref": route_token_ref,
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
    require_server_binding: bool = False,
    server_binding: Mapping[str, Any] | None = None,
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
            require_server_binding=require_server_binding,
            server_binding=server_binding or _route_token_server_binding(payload),
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


def _route_token_server_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    binding = (
        payload.get("server_binding")
        or payload.get("route_token_binding")
        or payload.get("route_token_server_binding")
    )
    if isinstance(binding, Mapping):
        return dict(binding)
    return {}


def _validate_route_token_server_binding(
    token: Mapping[str, Any],
    *,
    require_server_binding: bool,
    server_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not server_binding:
        if require_server_binding:
            raise MfSubagentContractError("route_token server binding is required")
        return {}
    if not isinstance(server_binding, Mapping):
        raise MfSubagentContractError("route_token server binding must be a mapping")

    route_token_ref = _string(server_binding.get("route_token_ref"))
    if not route_token_ref:
        raise MfSubagentContractError("route_token server binding missing route_token_ref")

    stored_allowed_actions = _route_allowed_actions(server_binding)
    token_allowed_actions = _route_allowed_actions(token)
    if not set(token_allowed_actions).issubset(set(stored_allowed_actions)):
        raise MfSubagentContractError(
            "route_token allowed_actions exceed server-issued binding grant"
        )

    for field in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
        "caller_role",
        "expires_at",
    ):
        expected = _string(server_binding.get(field))
        if not expected:
            continue
        if field == "caller_role":
            presented = _string(token.get("caller_role") or token.get("role")).lower()
            expected = expected.lower()
        else:
            presented = _string(token.get(field))
        if presented != expected:
            raise MfSubagentContractError(
                f"route_token {field} does not match server-issued binding"
            )

    binding_scope = (
        server_binding.get("scope")
        if isinstance(server_binding.get("scope"), Mapping)
        else {}
    )
    for field in ("project_id", "backlog_id", "task_id"):
        expected = _string(binding_scope.get(field) or server_binding.get(field))
        if not expected:
            continue
        if field == "backlog_id":
            presented = _route_scope_value(token, "backlog_id", "bug_id")
        else:
            presented = _route_scope_value(token, field)
        if presented != expected:
            raise MfSubagentContractError(
                f"route_token {field} scope does not match server-issued binding"
            )

    return {
        "server_issued_binding": True,
        "route_token_ref": route_token_ref,
        "binding_source": _string(server_binding.get("binding_source"))
        or "observer_route_token_refs",
    }


def _validate_route_token(
    token: Mapping[str, Any],
    *,
    action: str,
    request_scope: Mapping[str, str],
    now: datetime | None,
    require_server_binding: bool = False,
    server_binding: Mapping[str, Any] | None = None,
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

    # Content-hash verification for wired hash fields.
    # Each verify_content_hash call:
    # - Rejects forged/ill-formed hash strings (format floor).
    # - When the corresponding object is present in the token, recomputes and
    #   checks equality (content verify).  Object absent → format floor only.
    hash_verification: dict[str, Any] = {}

    if route_context_hash:
        route_context_obj = token.get("route_context")
        rv = verify_content_hash(
            route_context_hash,
            route_context_obj,
            field_name="route_context_hash",
        )
        hash_verification["route_context_hash"] = rv
        if not rv["ok"]:
            raise MfSubagentContractError(rv["reason"])

    if prompt_contract_hash:
        prompt_contract_obj = token.get("prompt_contract")
        rv = verify_content_hash(
            prompt_contract_hash,
            prompt_contract_obj,
            field_name="prompt_contract_hash",
        )
        hash_verification["prompt_contract_hash"] = rv
        if not rv["ok"]:
            raise MfSubagentContractError(rv["reason"])

    visible_injection_manifest_hash = _string(token.get("visible_injection_manifest_hash"))
    if visible_injection_manifest_hash:
        manifest_obj = token.get("visible_injection_manifest")
        rv = verify_content_hash(
            visible_injection_manifest_hash,
            manifest_obj,
            field_name="visible_injection_manifest_hash",
        )
        hash_verification["visible_injection_manifest_hash"] = rv
        if not rv["ok"]:
            raise MfSubagentContractError(rv["reason"])

    server_binding_result = _validate_route_token_server_binding(
        token,
        require_server_binding=require_server_binding,
        server_binding=server_binding,
    )

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
        "hash_verification": hash_verification,
        **server_binding_result,
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


def load_meta_contract_template(path: Path | None = None) -> dict[str, Any]:
    """Load the repo-owned meta-contract whitelist template."""

    template_path = (
        path
        if path is not None
        else Path(__file__).resolve().parent
        / "contract_templates"
        / _META_CONTRACT_TEMPLATE
    )
    try:
        data = json.loads(template_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MfSubagentContractError(
            f"meta-contract template not found: {template_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MfSubagentContractError(
            f"meta-contract template is not valid JSON: {template_path}"
        ) from exc
    if not isinstance(data, Mapping):
        raise MfSubagentContractError("meta-contract template must be a mapping")
    if _string(data.get("schema_version")) != META_CONTRACT_SCHEMA_VERSION:
        raise MfSubagentContractError(
            f"meta-contract template schema_version must be {META_CONTRACT_SCHEMA_VERSION}"
        )
    return dict(data)


def _meta_contract_containers(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [event]
    for key in ("payload", "verification", "artifact_refs"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
            for nested_key in (
                "mf_subagent_startup_gate",
                "startup_evidence",
                "bounded_startup_evidence",
                "mf_subagent_finish_gate",
                "route_token_gate",
                "route_token_gate_refusal",
                "service_route",
                "service_router",
            ):
                child = nested.get(nested_key)
                if isinstance(child, Mapping):
                    containers.append(child)
    return containers


def _meta_first_string(
    event: Mapping[str, Any],
    keys: Sequence[str],
) -> str:
    for container in _meta_contract_containers(event):
        for key in keys:
            token = _string(container.get(key))
            if token:
                return token
    return ""


def _meta_truthy_key(
    event: Mapping[str, Any],
    keys: Sequence[str],
) -> str:
    for container in _meta_contract_containers(event):
        for key in keys:
            if _bool(container.get(key)):
                return key
    return ""


def _meta_event_markers(event: Mapping[str, Any]) -> list[str]:
    markers: list[str] = []
    for key in ("event_kind", "event_type", "phase", "decision", "status"):
        marker = _normalized_action(event.get(key))
        if marker:
            markers.append(marker)
    for container in _meta_contract_containers(event):
        for key in ("action", "allowed_action", "requested_action", "gate_kind", "kind"):
            marker = _normalized_action(container.get(key))
            if marker:
                markers.append(marker)
    return _dedupe_strings(markers)


def _meta_action_from_event(event: Mapping[str, Any]) -> str:
    for marker in _meta_event_markers(event):
        if marker in _META_ACTION_ALIASES:
            return _META_ACTION_ALIASES[marker]
        if "work_mode_transition" in marker:
            return ""
        if marker.startswith("hotfix_") and marker != "hotfix_entered":
            return "hotfix_under_action"
        if marker.startswith("route_token_gate"):
            return "route_token_gate"
        if marker.startswith("service_route"):
            return "service_route"
        if marker.startswith("observer_command"):
            return "observer_command"
        if "no_progress_timeout" in marker:
            return "no_progress_timeout"
        if "bypass_rejected" in marker:
            return "forbidden_attempt_recorded"
        if "independent_verification" in marker:
            return "independent_verification"
        if marker.startswith("qa_"):
            return "qa_review" if "review" in marker else "qa_verification"
        if marker.startswith("merge_preview"):
            return "merge_preview"
        if marker.startswith("merge_queue"):
            return "merge_queue_entry"
        if marker.startswith("live_merge"):
            return "live_merge"
        if "reconcile" in marker:
            return "reconcile"
        if "blocker" in marker:
            return "record_blocker"
    return ""


def _meta_normalize_role(value: Any) -> str:
    role = _normalized_action(value)
    if not role:
        return ""
    if role in _META_ROLE_ALIASES:
        return _META_ROLE_ALIASES[role]
    if any(token in role for token in _META_SYSTEM_ROLE_TOKENS):
        return "system"
    if any(token in role for token in _META_JUDGE_ROLE_TOKENS):
        return "judge"
    if any(token in role for token in _META_OBSERVER_ROLE_TOKENS):
        return OBSERVER_COORDINATOR_ROLE
    if role.startswith("qa") or any(token in role for token in _META_QA_ROLE_TOKENS):
        return "qa"
    if any(token in role for token in _META_WORKER_ROLE_TOKENS):
        return MF_SUB_ROLE
    if "operator" in role:
        return "operator"
    return role


def _meta_role_from_event(event: Mapping[str, Any], *, action: str) -> str:
    actor = _normalized_action(event.get("actor"))
    if actor.startswith("observer_on_behalf_of"):
        return OBSERVER_COORDINATOR_ROLE
    actor_role = _meta_normalize_role(actor)
    if actor_role in {OBSERVER_COORDINATOR_ROLE, "qa", MF_SUB_ROLE, "operator", "judge", "system"}:
        return actor_role
    if action in {"service_route", "route_token_gate", "stale_artifact_cleanup"}:
        return "system"

    if action in {
        "implementation",
        "mf_subagent_startup",
        "read_receipt",
        "review_ready",
        "worker_progress",
        "patch",
    }:
        for key in ("worker_role", "lane_role", "actor_role", "role", "caller_role"):
            role = _meta_normalize_role(_meta_first_string(event, (key,)))
            if role:
                return role

    for key in ("caller_role", "role", "worker_role", "actor_role", "lane_role"):
        role = _meta_normalize_role(_meta_first_string(event, (key,)))
        if role:
            return role
    if action in {"independent_verification", "qa_verification", "qa_review"}:
        return "qa"
    if action in {
        "implementation",
        "mf_subagent_startup",
        "read_receipt",
        "review_ready",
        "worker_progress",
        "patch",
    }:
        return MF_SUB_ROLE
    if action == "forbidden_attempt_recorded" and actor_role not in {
        OBSERVER_COORDINATOR_ROLE,
        "judge",
        "system",
    }:
        return "system"
    return actor_role or "observer"


def _meta_on_behalf_present(event: Mapping[str, Any]) -> bool:
    actor = _normalized_action(event.get("actor"))
    if actor.startswith("observer_on_behalf_of"):
        return True
    if _bool(_meta_first_string(event, ("filed_on_behalf",))):
        return True
    return bool(_meta_first_string(event, _META_ON_BEHALF_KEYS))


def _meta_self_attesting_present(event: Mapping[str, Any]) -> bool:
    return bool(_meta_truthy_key(event, _META_SELF_ATTESTING_KEYS))


def _meta_surrogate_startup_present(event: Mapping[str, Any], *, action: str) -> bool:
    if action != "mf_subagent_startup":
        return False
    for container in _meta_contract_containers(event):
        token_type = _normalized_action(container.get("session_token_evidence_type"))
        match_mode = _normalized_action(container.get("agent_id_match_mode"))
        claims_close_satisfying = (
            _bool(container.get("close_satisfying"))
            or _bool(container.get("worker_self_attesting"))
            or _bool(container.get("self_attesting"))
        )
        if token_type in {"surrogate", "claimed_unverified"}:
            return claims_close_satisfying
        token_present = _bool(container.get("session_token_present"))
        if (
            match_mode == HOST_ADAPTER_SURROGATE_MATCH_MODE
            and (token_type != "server_verified" or not token_present)
        ):
            return claims_close_satisfying
        if _bool(container.get("host_adapter_startup_token_accepted")) and not _bool(
            container.get("session_token_present")
        ):
            return claims_close_satisfying
    return False


def _meta_allowed_actions(meta_contract: Mapping[str, Any], role: str) -> list[str]:
    whitelist = _mapping(
        meta_contract.get("role_action_whitelist"),
        field_name="role_action_whitelist",
    )
    role_policy = _mapping(whitelist.get(role), field_name=f"role_action_whitelist.{role}")
    return [
        _normalized_action(item)
        for item in _string_list_forgiving(role_policy.get("allowed_actions"))
        if _normalized_action(item)
    ]


def _meta_work_mode_transition_evidence_containers(
    event: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    queue = list(_meta_contract_containers(event))
    seen: set[int] = set()
    nested_keys = (
        "action_precheck",
        "bound_route_action_precheck",
        "canonical_route_identity",
        "route_action_precheck",
        "route_context",
        "route_identity",
        "work_mode_transition",
    )
    while queue:
        container = queue.pop(0)
        marker = id(container)
        if marker in seen:
            continue
        seen.add(marker)
        containers.append(container)
        for key in nested_keys:
            child = container.get(key)
            if isinstance(child, Mapping):
                queue.append(child)
    return containers


def _meta_work_mode_transition_missing_evidence(
    event: Mapping[str, Any],
) -> list[str]:
    containers = _meta_work_mode_transition_evidence_containers(event)
    missing: list[str] = []
    for key in _META_WORK_MODE_TRANSITION_ROUTE_IDENTITY_KEYS:
        if not any(_string(container.get(key)) for container in containers):
            missing.append(key)

    has_precheck_ref = any(
        _string(container.get(key))
        for container in containers
        for key in _META_WORK_MODE_TRANSITION_PRECHECK_REF_KEYS
    )
    if not has_precheck_ref:
        for container in containers:
            for key in (
                "action_precheck",
                "bound_route_action_precheck",
                "route_action_precheck",
            ):
                child = container.get(key)
                if _string(child):
                    has_precheck_ref = True
                    break
                if isinstance(child, Mapping) and any(
                    _string(child.get(ref_key))
                    for ref_key in (
                        "event_id",
                        "event_ref",
                        "id",
                        "ref",
                        "source_event_id",
                        "timeline_id",
                    )
                ):
                    has_precheck_ref = True
                    break
            if has_precheck_ref:
                break
    if not has_precheck_ref:
        missing.append("route_action_precheck_ref")
    return missing


def validate_meta_contract_timeline_event(
    event: Mapping[str, Any],
    *,
    meta_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one timeline event against the meta-contract role/action whitelist.

    This is intentionally event-model native: callers pass the same fields that
    are stored in ``task_timeline_events``.  Observer-authored events are not
    exempt; an observer can transport worker evidence only with explicit
    on-behalf metadata and without self-attesting worker claims.
    """

    if not isinstance(event, Mapping):
        raise MfSubagentContractError("meta-contract timeline event must be a mapping")
    event = dict(event)
    meta = dict(meta_contract or load_meta_contract_template())
    action = _meta_action_from_event(event)
    if not action:
        markers = ", ".join(_meta_event_markers(event))
        raise MfSubagentContractError(
            "meta-contract whitelist rejected unknown timeline action"
            + (f": {markers}" if markers else "")
        )
    role = _meta_role_from_event(event, action=action)
    forbidden_always = {
        _normalized_action(item)
        for item in _string_list_forgiving(meta.get("forbidden_always"))
    }
    markers = set(_meta_event_markers(event))
    marker_forbidden = sorted(forbidden_always.intersection(markers))
    forbidden_flag = _meta_truthy_key(event, _META_ALWAYS_FORBIDDEN_FLAG_KEYS)
    forbidden_evidence_actions = {
        _normalized_action(item)
        for item in _string_list_forgiving(
            meta.get("forbidden_evidence_recording_actions")
        )
    }
    may_record_forbidden_evidence = (
        action in forbidden_evidence_actions
        and role in {OBSERVER_COORDINATOR_ROLE, "judge", "system"}
    )
    if marker_forbidden and not may_record_forbidden_evidence:
        raise MfSubagentContractError(
            "meta-contract forbidden_always rejected action: "
            + ", ".join(marker_forbidden)
        )
    if forbidden_flag and not may_record_forbidden_evidence:
        raise MfSubagentContractError(
            f"meta-contract forbidden_always rejected flag: {forbidden_flag}"
        )
    if _meta_surrogate_startup_present(event, action=action):
        raise MfSubagentContractError(
            "meta-contract forbidden_always rejected surrogate_startup"
        )
    if action == "observer_work_mode_transition":
        missing_transition_evidence = _meta_work_mode_transition_missing_evidence(event)
        if missing_transition_evidence:
            raise MfSubagentContractError(
                "meta-contract observer_work_mode_transition missing required "
                "evidence: "
                + ", ".join(missing_transition_evidence)
            )

    worker_evidence_actions = {
        _normalized_action(item)
        for item in _string_list_forgiving(meta.get("worker_evidence_actions"))
    }
    on_behalf = _meta_on_behalf_present(event)
    self_attesting = _meta_self_attesting_present(event)
    observer_worker_transport = False
    if role == OBSERVER_COORDINATOR_ROLE and action in worker_evidence_actions:
        if not on_behalf:
            raise MfSubagentContractError(
                "meta-contract forbidden_always rejected author_worker_evidence: "
                "observer-authored worker evidence requires on_behalf_of"
            )
        if self_attesting:
            raise MfSubagentContractError(
                "meta-contract rejected observer on-behalf worker evidence with "
                "self_attesting=true"
            )
        observer_worker_transport = True

    allowed_actions = _meta_allowed_actions(meta, role)
    if (
        not observer_worker_transport
        and "*" not in allowed_actions
        and action not in allowed_actions
    ):
        raise MfSubagentContractError(
            f"meta-contract whitelist rejected role={role} action={action}"
        )

    return {
        "schema_version": META_CONTRACT_TIMELINE_GATE_SCHEMA_VERSION,
        "meta_contract_schema_version": META_CONTRACT_SCHEMA_VERSION,
        "meta_contract_id": _string(meta.get("id")) or META_CONTRACT_SCHEMA_VERSION,
        "meta_contract_hash": canonical_contract_hash(meta),
        "allowed": True,
        "status": "passed",
        "role": role,
        "action": action,
        "observer_event_validated": role == OBSERVER_COORDINATOR_ROLE,
        "on_behalf": on_behalf,
        "self_attesting": self_attesting,
        "observer_worker_transport": observer_worker_transport,
        "forbidden_always": sorted(forbidden_always),
        "allowed_actions": allowed_actions,
    }


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
    route_id = _dispatch_string(
        payload,
        names=("route_id",),
        nested_keys=(
            ("route_context", ("route_id", "id")),
            ("prompt_contract", ("route_id",)),
            ("route_prompt_contract", ("route_id",)),
        ),
    )
    route_token_ref = _dispatch_string(
        payload,
        names=("route_token_ref",),
        nested_keys=(
            ("route_context", ("route_token_ref",)),
            ("prompt_contract", ("route_token_ref",)),
            ("route_prompt_contract", ("route_token_ref",)),
            ("graph_identity", ("route_token_ref",)),
        ),
    )
    visible_injection_manifest_hash = _dispatch_string(
        payload,
        names=("visible_injection_manifest_hash",),
        nested_keys=(
            ("route_context", ("visible_injection_manifest_hash",)),
            ("prompt_contract", ("visible_injection_manifest_hash",)),
            ("route_prompt_contract", ("visible_injection_manifest_hash",)),
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
        "route_token_ref": route_token_ref,
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
        route_id=route_id,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
        route_token_ref=route_token_ref,
        visible_injection_manifest_hash=visible_injection_manifest_hash,
    )
    governed_evidence_required = _governed_nontrivial_graph_required(
        payload,
        parent_route_lineage=parent_route_lineage,
    )
    if governed_evidence_required and worker_role != MF_SUB_ROLE:
        raise MfSubagentContractError(
            "governed mf_sub dispatch requires worker_role=mf_sub"
        )
    graph_trace_source = _graph_trace_source(payload, allow_payload_fallback=False)
    graph_trace_ids = _dedupe_strings(
        trace_id
        for item in _deep_field_values(graph_trace_source, _GRAPH_TRACE_ID_KEYS)
        for trace_id in _trace_id_strings(item)
    )
    graph_trace_evidence = _normalize_graph_trace_evidence(
        payload,
        required=bool(governed_evidence_required and graph_trace_ids),
        task_id=task_id,
        parent_task_id=parent_task_id,
        worker_role=worker_role,
        fence_token=fence_token,
        allow_payload_fallback=False,
    )
    dispatch_graph_obligation = _normalize_dispatch_graph_obligation(
        payload,
        required=bool(governed_evidence_required and not graph_trace_ids),
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
        "route_id": route_id,
        "route_token_ref": route_token_ref,
        "visible_injection_manifest_hash": visible_injection_manifest_hash,
        "route_prompt_contract": child_route_prompt_contract,
        "parent_route_lineage": parent_route_lineage,
        "route_lineage": _mf_subagent_route_lineage(
            parent_route_lineage=parent_route_lineage,
            child_route_prompt_contract=child_route_prompt_contract,
        ),
        "governed_evidence_required": governed_evidence_required,
        "dispatch_graph_obligation": dispatch_graph_obligation,
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
    route_id: str = "",
    route_token_ref: str = "",
    visible_injection_manifest_hash: str = "",
    parent_route_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable input payload for a branch-isolated MF subagent."""

    _require_context(context)
    parent_task_id = _parent_task_id_for_contract_view(context)
    runtime_context_id = mf_subagent_runtime_context_id(context)
    child_route_prompt_contract = _child_route_prompt_contract(
        route_id=route_id,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
        route_token_ref=route_token_ref,
        visible_injection_manifest_hash=visible_injection_manifest_hash,
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
    # Extract real_startup_events from the finish-gate payload so the
    # surrogate join gate can check whether a real worker startup for the same
    # lane lineage exists.  Callers may pass timeline events under any of:
    # real_startup_events / startup_events / timeline_events / events.
    real_startup_events = (
        payload.get("real_startup_events")
        or payload.get("startup_events")
        or payload.get("timeline_events")
        or payload.get("events")
    )
    startup_present = _bounded_startup_evidence_present(
        startup_evidence, real_startup_events=real_startup_events
    )
    # Build a structured surrogate join result for the response.
    surrogate_join_gate = surrogate_startup_evidence_gate(
        startup_evidence,
        events=real_startup_events,
        real_startup_events=real_startup_events,
    )
    close_satisfying_startup_evidence = _close_satisfying_startup_evidence(
        startup_evidence,
        surrogate_join_gate=surrogate_join_gate,
    )
    startup_worker_identity_gate = _startup_worker_identity_close_gate(
        close_satisfying_startup_evidence
    )
    worker_self_attestation_gate = _finish_time_worker_attestation_gate(payload)
    observer_command_id = _dispatch_string(
        payload,
        names=("observer_command_id",),
        nested_keys=(
            ("worker_contract", ("observer_command_id",)),
            ("evidence", ("observer_command_id",)),
            ("startup_evidence", ("observer_command_id",)),
            ("mf_subagent_startup_gate", ("observer_command_id",)),
        ),
    ) or _string(
        close_satisfying_startup_evidence.get("observer_command_id")
        or startup_evidence.get("observer_command_id")
    )
    read_receipt_event_id = _dispatch_string(
        payload,
        names=("read_receipt_event_id", "read_receipt_timeline_id"),
        nested_keys=(
            ("read_receipt", ("event_id", "timeline_event_id", "timeline_id", "id")),
            ("worker_contract", ("read_receipt_event_id",)),
            ("evidence", ("read_receipt_event_id",)),
            ("startup_evidence", ("read_receipt_event_id",)),
            ("mf_subagent_startup_gate", ("read_receipt_event_id",)),
        ),
    ) or _string(
        close_satisfying_startup_evidence.get("read_receipt_event_id")
        or startup_evidence.get("read_receipt_event_id")
    )
    if not read_receipt_hash:
        read_receipt_hash = _string(
            close_satisfying_startup_evidence.get("read_receipt_hash")
            or startup_evidence.get("read_receipt_hash")
        )
    if not startup_present:
        # Produce a structured reason for the refusal to aid debugging.
        is_surrogate = _startup_is_host_adapter_surrogate(startup_evidence)
        if is_surrogate and surrogate_join_gate.get("real_worker_join"):
            join_reason = surrogate_join_gate["real_worker_join"].get("reason", "")
            raise MfSubagentContractError(
                "MF subagent finish gate requires actual mf_subagent_startup evidence "
                "before close-ready; surrogate-only startup is not close-satisfying "
                f"(join_result: {join_reason})"
            )
        raise MfSubagentContractError(
            "MF subagent finish gate requires actual mf_subagent_startup evidence "
            "before close-ready"
        )
    if not startup_worker_identity_gate["passed"]:
        blockers = ", ".join(startup_worker_identity_gate.get("blockers") or [])
        raise MfSubagentContractError(
            "MF subagent finish gate requires real startup worker identity before "
            f"close-ready ({blockers})"
        )
    if not worker_self_attestation_gate["passed"]:
        blockers = ", ".join(worker_self_attestation_gate.get("blockers") or [])
        raise MfSubagentContractError(
            "MF subagent finish gate requires finish-time worker_self_attestation "
            "before "
            f"close-ready ({blockers})"
        )
    if not observer_command_id:
        raise MfSubagentContractError(
            "MF subagent finish gate requires observer_command_id lineage before "
            "close-ready"
        )
    if not read_receipt_hash:
        raise MfSubagentContractError(
            "MF subagent finish gate requires mf_subagent_read_receipt before "
            "close-ready"
        )
    if not read_receipt_event_id:
        raise MfSubagentContractError(
            "MF subagent finish gate requires mf_subagent_read_receipt event lineage "
            "before close-ready"
        )

    child_route_prompt_contract = _merge_child_route_prompt_contract(
        child_route_prompt_contract,
        close_satisfying_startup_evidence,
    )
    route_identity = {
        "route_id": _string(child_route_prompt_contract.get("route_id")),
        "route_context_hash": _string(
            child_route_prompt_contract.get("route_context_hash")
        ),
        "prompt_contract_id": _string(
            child_route_prompt_contract.get("prompt_contract_id")
        ),
        "prompt_contract_hash": _string(
            child_route_prompt_contract.get("prompt_contract_hash")
        ),
        "route_token_ref": _string(child_route_prompt_contract.get("route_token_ref")),
        "visible_injection_manifest_hash": _string(
            child_route_prompt_contract.get("visible_injection_manifest_hash")
        ),
    }
    if not parent_task_id:
        parent_task_id = _string(
            close_satisfying_startup_evidence.get("parent_task_id")
            or startup_evidence.get("parent_task_id")
        )
    runtime_context_id = _string(
        close_satisfying_startup_evidence.get("runtime_context_id")
        or startup_evidence.get("runtime_context_id")
    )
    worker_id = _string(
        close_satisfying_startup_evidence.get("worker_id")
        or startup_evidence.get("worker_id")
        or close_satisfying_startup_evidence.get("worker_slot_id")
        or startup_evidence.get("worker_slot_id")
        or getattr(context, "worker_id", "")
    )
    worker_slot_id = _string(
        close_satisfying_startup_evidence.get("worker_slot_id")
        or startup_evidence.get("worker_slot_id")
        or worker_id
        or getattr(context, "worker_slot_id", "")
    )
    agent_id = _string(
        close_satisfying_startup_evidence.get("agent_id")
        or startup_evidence.get("agent_id")
        or getattr(context, "agent_id", "")
    )
    startup_worker_session_id = _string(
        startup_worker_identity_gate.get("worker_session_id")
        or close_satisfying_startup_evidence.get("worker_session_id")
        or startup_evidence.get("worker_session_id")
    )
    finish_worker_session_id = _string(
        worker_self_attestation_gate.get("worker_session_id")
    )
    worker_session_id = finish_worker_session_id or startup_worker_session_id
    worker_transcript_ref = _string(
        worker_self_attestation_gate.get("worker_transcript_ref")
        or startup_worker_identity_gate.get("worker_transcript_ref")
        or close_satisfying_startup_evidence.get("worker_transcript_ref")
        or startup_evidence.get("worker_transcript_ref")
    )
    worker_transcript_path = _string(
        worker_self_attestation_gate.get("worker_transcript_path")
        or startup_worker_identity_gate.get("worker_transcript_path")
        or close_satisfying_startup_evidence.get("worker_transcript_path")
        or startup_evidence.get("worker_transcript_path")
    )
    harness_type = _string(
        worker_self_attestation_gate.get("harness_type")
        or startup_worker_identity_gate.get("harness_type")
    )
    real_worker_join = _nested_mapping(surrogate_join_gate, "real_worker_join")
    startup_event_id = _string(
        close_satisfying_startup_evidence.get("startup_event_id")
        or startup_evidence.get("startup_event_id")
        or real_worker_join.get("join_event_id")
    )
    startup_event_kind = _string(
        close_satisfying_startup_evidence.get("startup_event_kind")
        or startup_evidence.get("startup_event_kind")
        or "mf_subagent_startup"
    )
    startup_lineage = {
        "schema_version": "mf_subagent_finish_gate_startup_lineage.v1",
        "startup_present": True,
        "close_satisfying": True,
        "startup_event_id": startup_event_id,
        "startup_event_kind": startup_event_kind,
        "startup_event_status": _string(
            close_satisfying_startup_evidence.get("startup_event_status")
            or startup_evidence.get("startup_event_status")
            or close_satisfying_startup_evidence.get("status")
            or startup_evidence.get("status")
        ),
        "gate_kind": _string(
            close_satisfying_startup_evidence.get("gate_kind")
            or startup_evidence.get("gate_kind")
        ),
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": context.backlog_id,
        "runtime_context_id": runtime_context_id,
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "worker_role": MF_SUB_ROLE,
        "fence_token": context.fence_token,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "head_commit": _string(
            close_satisfying_startup_evidence.get("head_commit")
            or startup_evidence.get("head_commit")
        ),
        "route_identity": route_identity,
        "observer_command_id": observer_command_id,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_event_id": read_receipt_event_id,
        "surrogate_joined": _bool(real_worker_join.get("joined")),
    }
    worker_identity = {
        "schema_version": "mf_subagent_finish_gate_worker_identity.v1",
        "worker_role": MF_SUB_ROLE,
        "role": MF_SUB_ROLE,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "runtime_context_id": runtime_context_id,
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "agent_id": agent_id,
        "worker_session_id": worker_session_id,
        "startup_worker_session_id": startup_worker_session_id,
        "finish_worker_session_id": finish_worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "harness_type": harness_type,
        "startup_identity_passed": True,
        "finish_attestation_passed": True,
    }
    route_lineage = _mf_subagent_route_lineage(
        parent_route_lineage=parent_route_lineage,
        child_route_prompt_contract=child_route_prompt_contract,
    )
    close_gate_projection = {
        "schema_version": FINISH_GATE_CLOSE_PROJECTION_SCHEMA_VERSION,
        "producer": "mf_subagent_worker",
        "source_event_kind": "mf_subagent_finish_gate",
        "implementation": True,
        "implementation_ready": True,
        "review_ready": True,
        "waiting_merge": True,
        "close_ready": True,
        "worker_status": "waiting_merge",
        "stop_state": "waiting_merge",
        "event_kinds_satisfied": ["implementation", "close_ready"],
        "evidence_ids": [
            "bounded_implementation_subagent.implementation",
            "bounded_implementation_subagent.review_ready",
            "bounded_implementation_subagent.waiting_merge",
            "bounded_implementation_subagent.close_ready",
        ],
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": context.backlog_id,
        "runtime_context_id": runtime_context_id,
        "checkpoint_id": checkpoint_id,
        "commit": claimed_head or context.head_commit,
        "head_commit": claimed_head or context.head_commit,
        "merge_queue_id": context.merge_queue_id,
        "fence_token": context.fence_token,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "changed_files": normalized["changed_files"],
        "new_files": normalized["new_files"],
        "test_results": normalized["test_results"],
        "route_identity": route_identity,
        "route_lineage": route_lineage,
        "observer_command_id": observer_command_id,
        "worker_identity": worker_identity,
        "startup_lineage": startup_lineage,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_event_id": read_receipt_event_id,
        "graph_trace_evidence": graph_trace_evidence,
    }
    lane_ownership_projection = {
        "schema_version": FINISH_GATE_LANE_OWNERSHIP_PROJECTION_SCHEMA_VERSION,
        "evidence_id": "bounded_implementation_subagent.review_ready",
        "evidence_ids": [
            "bounded_implementation_subagent.implementation",
            "bounded_implementation_subagent.review_ready",
            "bounded_implementation_subagent.waiting_merge",
            "bounded_implementation_subagent.close_ready",
        ],
        "implementation": True,
        "implementation_ready": True,
        "review_ready": True,
        "waiting_merge": True,
        "close_ready": True,
        "worker_status": "waiting_merge",
        "stop_state": "waiting_merge",
        "worker_role": MF_SUB_ROLE,
        "role": MF_SUB_ROLE,
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "agent_id": agent_id,
        "worker_session_id": worker_session_id,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "backlog_id": context.backlog_id,
        "runtime_context_id": runtime_context_id,
        "checkpoint_id": checkpoint_id,
        "merge_queue_id": context.merge_queue_id,
        "fence_token": context.fence_token,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "head_commit": claimed_head or context.head_commit,
        "commit": claimed_head or context.head_commit,
        "changed_files": normalized["changed_files"],
        "new_files": normalized["new_files"],
        "route_id": _string(child_route_prompt_contract.get("route_id")),
        "route_context_hash": _string(
            child_route_prompt_contract.get("route_context_hash")
        ),
        "prompt_contract_id": _string(
            child_route_prompt_contract.get("prompt_contract_id")
        ),
        "prompt_contract_hash": _string(
            child_route_prompt_contract.get("prompt_contract_hash")
        ),
        "route_token_ref": _string(child_route_prompt_contract.get("route_token_ref")),
        "visible_injection_manifest_hash": _string(
            child_route_prompt_contract.get("visible_injection_manifest_hash")
        ),
        "parent_route_id": _string(route_lineage.get("parent_route_id")),
        "parent_route_context_hash": _string(
            route_lineage.get("parent_route_context_hash")
        ),
        "parent_prompt_contract_id": _string(
            route_lineage.get("parent_prompt_contract_id")
        ),
        "route_identity": route_identity,
        "observer_command_id": observer_command_id,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_event_id": read_receipt_event_id,
        "worker_identity": worker_identity,
        "startup_lineage": startup_lineage,
        "producer": "mf_subagent_worker",
        "source_event_kind": "mf_subagent_finish_gate",
    }

    return {
        "schema_version": FINISH_GATE_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "project_id": context.project_id,
        "governance_project_id": context.governance_project_id or context.project_id,
        "target_project_id": context.target_project_id or context.project_id,
        "target_project_root": context.target_project_root,
        "task_id": context.task_id,
        "parent_task_id": parent_task_id,
        "worker_role": MF_SUB_ROLE,
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "agent_id": agent_id,
        "runtime_context_id": runtime_context_id,
        "worker_session_id": worker_session_id,
        "observer_command_id": observer_command_id,
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
        "route_identity": route_identity,
        "route_id": _string(child_route_prompt_contract.get("route_id")),
        "route_context_hash": _string(
            child_route_prompt_contract.get("route_context_hash")
        ),
        "prompt_contract_id": _string(
            child_route_prompt_contract.get("prompt_contract_id")
        ),
        "prompt_contract_hash": _string(
            child_route_prompt_contract.get("prompt_contract_hash")
        ),
        "route_token_ref": _string(child_route_prompt_contract.get("route_token_ref")),
        "visible_injection_manifest_hash": _string(
            child_route_prompt_contract.get("visible_injection_manifest_hash")
        ),
        "parent_route_lineage": parent_route_lineage,
        "route_lineage": route_lineage,
        "governed_evidence_required": governed_evidence_required,
        "startup_evidence": startup_evidence,
        "close_satisfying_startup_evidence": close_satisfying_startup_evidence,
        "startup_lineage": startup_lineage,
        "surrogate_join_gate": surrogate_join_gate,
        "startup_worker_identity_gate": startup_worker_identity_gate,
        "worker_self_attestation_gate": worker_self_attestation_gate,
        "worker_self_attestation": worker_self_attestation_gate.get("attestation", {}),
        "worker_identity": worker_identity,
        "read_receipt_hash": read_receipt_hash,
        "read_receipt_event_id": read_receipt_event_id,
        "gate_receipt_hash": gate_receipt_hash,
        "receipt_gate": {
            "schema_version": "mf_subagent_receipt_gate.v1",
            "read_receipt_required_before_counted_evidence": bool(
                governed_evidence_required or graph_trace_evidence.get("present")
            ),
            "read_receipt_present": bool(read_receipt_hash),
            "read_receipt_event_id_present": bool(read_receipt_event_id),
            "observer_command_id_present": bool(observer_command_id),
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
        "implementation": True,
        "implementation_ready": True,
        "review_ready": True,
        "waiting_merge": True,
        "worker_status": "waiting_merge",
        "stop_state": "waiting_merge",
        "lane_ownership_projection": lane_ownership_projection,
        "close_gate_projection": close_gate_projection,
        "close_ready": True,
    }
