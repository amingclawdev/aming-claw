"""Runtime-context worker proof helpers.

This module stays side-effect free: it validates copy-safe mf_sub runtime
identity and prepares public provenance for ContractRuntime line writes. HTTP
facades remain in ``server.py``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


CONTRACT_RUNTIME_WORKER_PROOF_SCHEMA_VERSION = (
    "contract_runtime.worker_copy_safe_proof.v1"
)
CONTRACT_RUNTIME_WORKER_PROOF_REQUIRED_FIELDS = (
    "runtime_context_id",
    "session_token_ref",
    "parent_task_id",
    "target_project_root",
)
CONTRACT_RUNTIME_WORKER_WRITE_PROOF_REQUIRED_FIELDS = (
    *CONTRACT_RUNTIME_WORKER_PROOF_REQUIRED_FIELDS,
    "fence_token",
)
CONTRACT_RUNTIME_WORKER_PROVENANCE_SCHEMA_VERSION = (
    "contract_runtime.worker_evidence_provenance.v1"
)


class RuntimeContextWorkerProofError(ValueError):
    """Structured blocker for a missing or invalid mf_sub worker proof."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _role(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(".", "_")


def mf_sub_parent_task_id(context: Any) -> str:
    """Return the canonical parent task id for an mf_sub runtime context."""

    parent_task_id = _text(getattr(context, "parent_task_id", ""))
    if parent_task_id:
        return parent_task_id
    root_task_id = _text(getattr(context, "root_task_id", ""))
    chain_id = _text(getattr(context, "chain_id", ""))
    task_id = _text(getattr(context, "task_id", ""))
    if (
        chain_id
        and chain_id not in {root_task_id, task_id}
        and not chain_id.startswith(("chain-", "cchain-"))
    ):
        return chain_id
    stage_task_id = _text(getattr(context, "stage_task_id", ""))
    return _text(
        root_task_id
        or stage_task_id
        or chain_id
        or getattr(context, "backlog_id", "")
        or task_id
    )


def _raise_proof_error(
    code: str,
    message: str,
    *,
    runtime_context_id: str = "",
    missing_required_fields: Sequence[str] = (),
    required_fields: Sequence[str] = CONTRACT_RUNTIME_WORKER_PROOF_REQUIRED_FIELDS,
    **details: Any,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": CONTRACT_RUNTIME_WORKER_PROOF_SCHEMA_VERSION,
        "required_role": "mf_sub",
        "proof_error": code,
        "runtime_context_id": runtime_context_id,
        "required_fields": list(required_fields),
        "missing_required_fields": list(missing_required_fields),
        "raw_session_token_required": False,
        "raw_route_token_required": False,
    }
    payload.update(details)
    raise RuntimeContextWorkerProofError(code, message, payload)


def _allowed_parent_ids(context: Any) -> set[str]:
    return {
        value
        for value in (
            mf_sub_parent_task_id(context),
            _text(getattr(context, "parent_task_id", "")),
            _text(getattr(context, "root_task_id", "")),
            _text(getattr(context, "chain_id", "")),
            _text(getattr(context, "stage_task_id", "")),
            _text(getattr(context, "task_id", "")),
            _text(getattr(context, "backlog_id", "")),
        )
        if value
    }


def validate_contract_runtime_worker_proof(
    conn,
    *,
    project_id: str,
    runtime_context_id: str,
    session_token_ref: str,
    parent_task_id: str,
    target_project_root: str,
    task_id: str = "",
    worker_role: str = "",
    fence_token: str = "",
    session_token: str = "",
    governance_project_id: str = "",
    target_project_id: str = "",
    require_fence_token: bool = False,
    allow_worktree_target_root_alias: bool = True,
    allowed_statuses: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate copy-safe ContractRuntime proof for a resumed mf_sub worker."""

    runtime_context_id = _text(runtime_context_id)
    session_token_ref = _text(session_token_ref)
    parent_task_id = _text(parent_task_id)
    target_project_root = _text(target_project_root)
    task_id = _text(task_id)
    worker_role = _role(worker_role or "mf_sub")
    fence_token = _text(fence_token)
    session_token = _text(session_token)
    required_fields = (
        CONTRACT_RUNTIME_WORKER_WRITE_PROOF_REQUIRED_FIELDS
        if require_fence_token
        else CONTRACT_RUNTIME_WORKER_PROOF_REQUIRED_FIELDS
    )
    missing = [
        field
        for field, value in (
            ("runtime_context_id", runtime_context_id),
            ("session_token_ref", session_token_ref or session_token),
            ("parent_task_id", parent_task_id),
            ("target_project_root", target_project_root),
            ("fence_token", fence_token if require_fence_token else "not-required"),
        )
        if field in required_fields and not value
    ]
    if missing:
        _raise_proof_error(
            "missing_worker_proof_fields",
            "ContractRuntime mf_sub worker proof is missing required fields",
            runtime_context_id=runtime_context_id,
            missing_required_fields=missing,
            required_fields=required_fields,
        )
    if worker_role != "mf_sub":
        _raise_proof_error(
            "worker_role_mismatch",
            "ContractRuntime worker proof requires worker_role=mf_sub",
            runtime_context_id=runtime_context_id,
            worker_role=worker_role,
            expected_worker_role="mf_sub",
            required_fields=required_fields,
        )

    from .parallel_branch_runtime import (
        ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES,
        BranchRuntimeFenceError,
        STATE_VALIDATED,
        ensure_branch_runtime_schema,
        get_branch_context_by_runtime_context_id,
        mf_subagent_session_token_hash,
        runtime_context_effective_target_project_root,
        runtime_context_id_for_branch_context,
        runtime_context_secret_hash,
        runtime_context_session_token_ref,
        runtime_context_session_token_ref_matches,
        runtime_context_target_project_root_matches,
        utc_now,
        validate_mf_subagent_runtime_context_lookup,
    )

    ensure_branch_runtime_schema(conn)
    context_project_id = _text(governance_project_id or project_id)
    context = get_branch_context_by_runtime_context_id(
        conn,
        context_project_id,
        runtime_context_id,
    )
    if context is None:
        _raise_proof_error(
            "runtime_context_not_found",
            "ContractRuntime mf_sub worker proof references an unknown runtime context",
            runtime_context_id=runtime_context_id,
            required_fields=required_fields,
        )

    resolved_runtime_context_id = runtime_context_id_for_branch_context(context)
    if resolved_runtime_context_id != runtime_context_id:
        _raise_proof_error(
            "runtime_context_id_mismatch",
            "ContractRuntime mf_sub worker proof runtime_context_id does not match",
            runtime_context_id=runtime_context_id,
            expected_runtime_context_id=resolved_runtime_context_id,
            required_fields=required_fields,
        )
    if task_id and task_id != _text(getattr(context, "task_id", "")):
        _raise_proof_error(
            "runtime_context_task_mismatch",
            "ContractRuntime mf_sub worker proof task_id does not match",
            runtime_context_id=runtime_context_id,
            task_id=task_id,
            expected_task_id=_text(getattr(context, "task_id", "")),
            required_fields=required_fields,
        )
    parent_ids = _allowed_parent_ids(context)
    if parent_ids and parent_task_id not in parent_ids:
        _raise_proof_error(
            "runtime_context_parent_task_mismatch",
            "ContractRuntime mf_sub worker proof parent_task_id does not match",
            runtime_context_id=runtime_context_id,
            parent_task_id=parent_task_id,
            expected_parent_task_id=mf_sub_parent_task_id(context),
            accepted_parent_task_ids=sorted(parent_ids),
            required_fields=required_fields,
        )

    raw_session_matches = bool(
        session_token
        and _text(getattr(context, "session_token_hash", ""))
        and mf_subagent_session_token_hash(session_token)
        == _text(getattr(context, "session_token_hash", ""))
    )
    if not (
        runtime_context_session_token_ref_matches(context, session_token_ref)
        or raw_session_matches
    ):
        _raise_proof_error(
            "session_token_ref_mismatch",
            "ContractRuntime mf_sub worker proof session token does not match",
            runtime_context_id=runtime_context_id,
            session_token_ref_present=bool(session_token_ref),
            session_token_present=bool(session_token),
            required_fields=required_fields,
        )

    if _text(getattr(context, "lease_expires_at", "")) and (
        _text(getattr(context, "lease_expires_at", "")) < utc_now()
    ):
        _raise_proof_error(
            "runtime_session_token_expired",
            "ContractRuntime mf_sub worker proof session lease has expired",
            runtime_context_id=runtime_context_id,
            lease_expires_at=_text(getattr(context, "lease_expires_at", "")),
            required_fields=required_fields,
        )

    allowed = set(allowed_statuses or {*ACTIVE_MF_SUBAGENT_GRAPH_QUERY_STATES, STATE_VALIDATED})
    if _text(getattr(context, "status", "")) not in allowed:
        _raise_proof_error(
            "runtime_context_status_not_active",
            "ContractRuntime mf_sub worker proof runtime context is not active",
            runtime_context_id=runtime_context_id,
            status=_text(getattr(context, "status", "")),
            allowed_statuses=sorted(allowed),
            required_fields=required_fields,
        )

    if not runtime_context_target_project_root_matches(
        context,
        target_project_root,
        allow_worktree_alias=allow_worktree_target_root_alias,
    ):
        _raise_proof_error(
            "target_project_root_mismatch",
            "ContractRuntime mf_sub worker proof target_project_root does not match",
            runtime_context_id=runtime_context_id,
            target_project_root=target_project_root,
            expected_target_project_root=runtime_context_effective_target_project_root(
                context
            ),
            worktree_path=_text(getattr(context, "worktree_path", "")),
            required_fields=required_fields,
        )

    if require_fence_token:
        try:
            validate_mf_subagent_runtime_context_lookup(
                conn,
                project_id=project_id,
                runtime_context_id=runtime_context_id,
                parent_task_id=parent_task_id,
                worker_role="mf_sub",
                fence_token=fence_token,
                governance_project_id=governance_project_id or project_id,
                target_project_id=target_project_id or project_id,
                target_project_root=target_project_root,
                session_token=session_token,
                session_token_ref=session_token_ref,
                allowed_statuses=tuple(allowed),
                allow_worktree_target_root_alias=allow_worktree_target_root_alias,
            )
        except BranchRuntimeFenceError as exc:
            _raise_proof_error(
                str(exc) or "fence_invalidated_or_unknown",
                "ContractRuntime mf_sub worker proof fence validation failed",
                runtime_context_id=runtime_context_id,
                required_fields=required_fields,
            )

    worker_id = _text(getattr(context, "worker_id", ""))
    worker_slot_id = _text(getattr(context, "worker_slot_id", "")) or worker_id
    return {
        "schema_version": CONTRACT_RUNTIME_WORKER_PROOF_SCHEMA_VERSION,
        "role": "mf_sub",
        "role_source": "runtime_context_copy_safe_worker_proof",
        "runtime_context_id": runtime_context_id,
        "task_id": _text(getattr(context, "task_id", "")),
        "parent_task_id": mf_sub_parent_task_id(context),
        "worker_role": "mf_sub",
        "worker_id": worker_id,
        "worker_slot_id": worker_slot_id,
        "target_project_root": runtime_context_effective_target_project_root(context),
        "worktree_path": _text(getattr(context, "worktree_path", "")),
        "session_token_ref": session_token_ref or runtime_context_session_token_ref(context),
        "session_token_ref_present": bool(
            session_token_ref or runtime_context_session_token_ref(context)
        ),
        "fence_token_hash": runtime_context_secret_hash(
            _text(getattr(context, "fence_token", ""))
        ),
        "fence_token_present": bool(fence_token),
        "fence_token_redacted": True,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "context": context,
    }


def worker_proof_line_provenance(proof: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return public worker-owned evidence provenance for a verified proof."""

    if not isinstance(proof, Mapping) or not proof:
        return {}
    return {
        "schema_version": CONTRACT_RUNTIME_WORKER_PROVENANCE_SCHEMA_VERSION,
        "source": "runtime_context_copy_safe_worker_proof",
        "verified": True,
        "worker_owned": True,
        "observer_impersonation": False,
        "runtime_context_id": _text(proof.get("runtime_context_id")),
        "task_id": _text(proof.get("task_id")),
        "parent_task_id": _text(proof.get("parent_task_id")),
        "worker_role": "mf_sub",
        "worker_id": _text(proof.get("worker_id")),
        "worker_slot_id": _text(proof.get("worker_slot_id")),
        "target_project_root": _text(proof.get("target_project_root")),
        "session_token_ref": _text(proof.get("session_token_ref")),
        "session_token_ref_present": bool(proof.get("session_token_ref")),
        "fence_token_hash": _text(proof.get("fence_token_hash")),
        "fence_token_redacted": True,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
    }


def attach_contract_runtime_worker_provenance(
    write: Mapping[str, Any],
    proof: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach verified mf_sub provenance to a ContractRuntime line write."""

    line = dict(write)
    provenance = worker_proof_line_provenance(proof)
    if not provenance:
        return line
    worker_id = provenance.get("worker_slot_id") or provenance.get("worker_id") or "mf_sub"
    line.update(
        {
            "runtime_context_id": provenance["runtime_context_id"],
            "task_id": provenance["task_id"],
            "parent_task_id": provenance["parent_task_id"],
            "worker_role": "mf_sub",
            "worker_id": provenance["worker_id"],
            "worker_slot_id": provenance["worker_slot_id"],
            "authorization_source": "runtime_context_copy_safe_worker_proof",
            "actor_session_principal": worker_id,
            "evidence_owner_actor": worker_id,
            "evidence_owner_role": "mf_sub",
            "evidence_owner_session_ref": provenance["session_token_ref"],
            "submitter_principal": worker_id,
            "submitter_session": provenance["session_token_ref"],
            "observer_impersonation": False,
            "worker_evidence_provenance": provenance,
        }
    )
    payload = dict(line.get("payload")) if isinstance(line.get("payload"), Mapping) else {}
    payload.update(
        {
            "worker_evidence_provenance": provenance,
            "observer_impersonation": False,
        }
    )
    line["payload"] = payload
    artifact_refs = (
        dict(line.get("artifact_refs"))
        if isinstance(line.get("artifact_refs"), Mapping)
        else {}
    )
    artifact_refs.setdefault("runtime_context_id", provenance["runtime_context_id"])
    artifact_refs.setdefault("task_id", provenance["task_id"])
    artifact_refs.setdefault("worker_role", "mf_sub")
    artifact_refs["worker_evidence_provenance"] = provenance
    line["artifact_refs"] = artifact_refs
    return line
