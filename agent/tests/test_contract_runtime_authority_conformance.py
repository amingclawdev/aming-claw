from __future__ import annotations

import inspect
import json

from agent.governance import server
from agent.governance.worker_transcript_verify import verify_worker_transcript


def _writer_safe_copy() -> dict[str, object]:
    return {
        "schema_version": "contract_runtime.writer_role_safe_copy_payload.v1",
        "tool": "contract_runtime_submit_line",
        "copy_payload": {
            "contract_execution_id": "cex-authority-conformance",
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "actor_role": "mf_sub",
            "evidence_kind": "implementation",
            "execution_state_revision": 12,
            "runtime_guide_hash": "sha256:" + "a" * 64,
            "runtime_context_id": "mfrctx-authority-conformance",
            "task_id": "MF-AUTHORITY-CONFORMANCE",
            "line_instance_id": (
                "runtime_context:mfrctx-authority-conformance"
            ),
        },
    }


def test_bounded_current_projection_preserves_exact_worker_writer_payload():
    writer_safe_copy = _writer_safe_copy()
    source = {
        "line_id": "worker_implementation",
        "owner_role": "mf_sub",
        "submit_line_guidance": {"copy_payload_available": True},
        "writer_role_safe_copy_payload": writer_safe_copy,
        "diagnostic_detail": (
            "x" * (server._RUNTIME_CONTEXT_SERVER_READ_INLINE_BYTES + 1)
        ),
    }

    projected = server._runtime_context_server_bounded_mapping(
        source,
        field="contract_runtime_next_legal_action",
    )

    assert projected["bounded_projection"] is True
    assert projected["writer_role_safe_copy_payload"] == writer_safe_copy


def test_bounded_worker_guide_preserves_exact_worker_writer_payload():
    writer_safe_copy = _writer_safe_copy()
    source = {
        "line_id": "worker_implementation",
        "owner_role": "mf_sub",
        "submit_line_guidance": {"copy_payload_available": True},
        "writer_role_safe_copy_payload": writer_safe_copy,
        "diagnostic_detail": (
            "x"
            * (
                server._RUNTIME_CONTEXT_WORKER_GUIDE_INLINE_DIAGNOSTIC_BYTES
                + 1
            )
        ),
    }

    projected, page = server._runtime_context_worker_guide_paged_value(
        source,
        field="contract_runtime_next_legal_action",
        detail_ref="worker-guide-detail:authority-conformance",
    )

    assert page
    assert projected["submit_line_guidance"]["copy_payload_available"] is True
    assert projected["writer_role_safe_copy_payload"] == writer_safe_copy


def test_finish_transcript_does_not_persist_or_require_raw_fence_bearer():
    raw_fence = "fence-process-local-secret"
    result = verify_worker_transcript(
        {
            "attestation_phase": "finish",
            "worker_session_id": "worker-authority-conformance",
            "worker_transcript_ref": "codex:worker-authority-conformance",
            "harness_type": "codex",
            "task_id": "MF-AUTHORITY-CONFORMANCE",
            "runtime_context_id": "mfrctx-authority-conformance",
            "fence_token": raw_fence,
            "worktree_path": "/tmp/authority-conformance",
            "branch_ref": "refs/heads/authority-conformance",
        }
    )

    runtime_layer = next(
        layer for layer in result["layers"] if layer["id"] == "runtime_lane_match"
    )
    assert "fence_token" not in runtime_layer["runtime_fields"]
    assert raw_fence not in json.dumps(result, sort_keys=True)


def test_implementation_bypass_continuation_accepts_bound_lane_task_event_id():
    anchor_source = "".join(
        inspect.getsource(
            server._contract_runtime_worker_implementation_bypass_continuation_anchor
        ).split()
    )
    strict_audit_source = inspect.getsource(
        server._contract_runtime_strict_no_pass_bypass_audit
    )

    assert "expected_event_task_ids={execution_id,task_id}" in anchor_source
    assert '"runtime_context_id":runtime_context_id' in "".join(
        strict_audit_source.split()
    )
    assert '"task_id":task_id' in "".join(strict_audit_source.split())
    assert '"line_instance_id":line_instance_id' in "".join(
        strict_audit_source.split()
    )


def test_implementation_bypass_continuation_uses_persisted_fence_verifier():
    anchor_source = "".join(
        inspect.getsource(
            server._contract_runtime_worker_implementation_bypass_continuation_anchor
        ).split()
    )

    assert "runtime_context_fence_token_verifier(context)" in anchor_source
    assert 'getattr(context,"fence_token","")' not in anchor_source
