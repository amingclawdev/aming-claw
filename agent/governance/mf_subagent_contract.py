"""Backend-neutral contract for MF subagent branch workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.governance.parallel_branch_runtime import BranchTaskRuntimeContext


MF_SUB_ROLE = "mf_sub"
INPUT_SCHEMA_VERSION = "mf_subagent_input.v1"
RESULT_SCHEMA_VERSION = "mf_subagent_result.v1"
FINISH_GATE_SCHEMA_VERSION = "mf_subagent_finish_gate.v1"
FINISH_GATE_REPLAY_SOURCE = "mf_sub_finish_gate"
BACKEND_CONTRACT = "parallel_branch_worker.v1"

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
    "fence_token",
)
_READY_STATUSES = {"completed", "succeeded", "ready_for_merge"}
_PASS_STATUSES = {"pass", "passed", "ok", "succeeded", "success", "clean"}
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
)


class MfSubagentContractError(ValueError):
    """Raised when an MF subagent payload violates the worker contract."""


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
) -> dict[str, Any]:
    """Build the stable input payload for a branch-isolated MF subagent."""

    _require_context(context)
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "backend": backend,
        "backend_contract": BACKEND_CONTRACT,
        "project_id": context.project_id,
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
            "agent_id": context.agent_id,
            "worker_id": context.worker_id,
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
        "capabilities": {
            "can": list(MF_SUB_ALLOWED_CAPABILITIES),
            "cannot": list(MF_SUB_FORBIDDEN_ACTIONS),
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

    return {
        "schema_version": FINISH_GATE_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "project_id": context.project_id,
        "task_id": context.task_id,
        "backlog_id": context.backlog_id,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "head_commit": claimed_head or context.head_commit,
        "checkpoint_id": checkpoint_id,
        "fence_token": context.fence_token,
        "replay_source": FINISH_GATE_REPLAY_SOURCE,
        "changed_files": normalized["changed_files"],
        "new_files": normalized["new_files"],
        "test_results": normalized["test_results"],
        "blockers": normalized["blockers"],
        "summary": normalized["summary"],
        "evidence": normalized["evidence"],
        "merge_queue_ready": True,
    }
