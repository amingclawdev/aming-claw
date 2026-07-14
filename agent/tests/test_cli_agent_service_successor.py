from __future__ import annotations

import hashlib
import json

from agent.cli_agent_service.evidence import CliAgentRunReceipt
from agent.governance.contract_state_runtime import (
    build_cli_agent_successor_ticket,
)
from agent.governance.server import _cli_agent_persisted_receipt_event


def _authority_and_launch():
    launch = {
        "project_id": "aming-claw",
        "backlog_id": "AC-CLI-SUCCESSOR",
        "task_id": "task-successor",
        "parent_task_id": "observer-successor",
        "runtime_context_id": "mfrctx-successor-worker",
        "worker_role": "mf_sub",
        "worktree_path": "/tmp/successor-worker",
        "branch_ref": "refs/heads/codex/successor-worker",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "merge_queue_id": "mq-successor",
        "owned_files": ["agent/worker.py"],
        "route_id": "route-successor",
        "route_context_hash": "sha256:route-successor",
        "prompt_contract_id": "rprompt-successor",
        "prompt_contract_hash": "sha256:prompt-successor",
        "route_token_ref": "rtok-successor",
        "visible_injection_manifest_hash": "sha256:manifest-successor",
    }
    authority = {
        "schema_version": "contract_runtime_current_state.v1",
        "source_of_authority": "ContractRuntime",
        "authority_decision_source": "contract_runtime_completed_dispatch_line",
        "project_id": "aming-claw",
        "backlog_id": "AC-CLI-SUCCESSOR",
        "contract_execution_id": "cex-cli-successor",
        "contract_revision_id": "rev-cli-successor",
        "execution_state_revision": 9,
        "execution_state_hash": "sha256:state-cli-successor",
        "runtime_guide_hash": "sha256:guide-cli-successor",
        "readiness_state": "contract_active",
        "next_legal_action": {
            "id": "successor_worker_dispatch",
            "action": "dispatch_successor",
            **{
                key: value
                for key, value in launch.items()
                if key not in {"project_id", "backlog_id", "worktree_path"}
            },
            "target_project_root": launch["worktree_path"],
            "profile_requirements": {
                "profile_id": "codex-profile-a",
                "role": "observer",
                "harness": "codex",
                "provider": "openai",
                "successor_budget": 2,
            },
            "retry_policy": {
                "attempt": 1,
                "max_attempts": 3,
                "on_crash": "create_successor",
                "successor_required": True,
            },
        },
    }
    return authority, launch


def _lost_receipt():
    return CliAgentRunReceipt(
        run_id="run-lost-observer",
        state="lost",
        event_index=3,
        observed_at="2026-07-13T00:00:00Z",
        ticket_id="caet-1234567890abcdef12345678",
        ticket_hash="sha256:" + hashlib.sha256(b"ticket").hexdigest(),
        profile_id="codex-profile-a",
        runtime_context_id="mfrctx-lost-observer",
        command_hash="sha256:" + hashlib.sha256(b"command").hexdigest(),
        process_identity={
            "pid": 4242,
            "process_group_id": 4242,
            "process_start_identity_hash": "sha256:"
            + hashlib.sha256(b"process").hexdigest(),
        },
        output_hash="sha256:" + hashlib.sha256(b"").hexdigest(),
        duration_ms=5000,
        exit_code=None,
        failure_category="lost",
    ).to_public_dict()


def _inputs(*, role="observer", failed_principal="observer-1"):
    authority, launch = _authority_and_launch()
    authority["next_legal_action"]["worker_role"] = role
    authority["next_legal_action"]["profile_requirements"]["role"] = role
    launch["worker_role"] = role
    failed = {
        "run_id": "run-lost-observer",
        "profile_id": "codex-profile-a",
        "role": role,
        "parent_run_id": "jb-root",
        "principal_id": failed_principal,
        "credential": "must-not-appear",
    }
    successor = {
        "run_id": "run-successor-observer",
        "profile_id": "codex-profile-a",
        "role": role,
        "parent_run_id": "jb-root",
        "successor_of_run_id": failed["run_id"],
        "principal_id": "observer-2",
        "prompt": "must-not-appear",
    }
    return {
        "contract_runtime_current_state": authority,
        "lost_run_receipt": _lost_receipt(),
        "lost_receipt_event_ref": "timeline:9",
        "failed_run": failed,
        "successor_run": successor,
        "successor_launch_identity": launch,
        "role_policy": {"successor_budget": 2},
        "failure_evidence": {
            "category": "heartbeat_timeout",
            "evidence_ref": "timeline:9",
        },
        "checkpoint_evidence": {
            "checkpoint_id": "checkpoint-observer-8",
            "checkpoint_hash": "sha256:checkpoint-observer-8",
            "evidence_ref": "timeline:10",
            "status": "passed",
        },
        "retry_state": {
            "attempt": 1,
            "max_attempts": 3,
            "successor_count": 0,
            "loop_count": 0,
            "max_loops": 2,
        },
        "requester_notification": {
            "notification_id": "notify-jb-1",
            "notification_ref": "timeline:11",
            "status": "recorded",
            "channel": "task_timeline",
            "target_role": "requester",
        },
        "expected_execution_state_revision": 9,
        "expected_execution_state_hash": "sha256:state-cli-successor",
    }


def test_l2_lost_run_issues_new_lineaged_successor_ticket():
    ticket = build_cli_agent_successor_ticket(**_inputs())

    assert ticket["status"] == "issued"
    assert ticket["issue_allowed"] is True
    assert ticket["source_of_authority"] == "ContractRuntime"
    assert ticket["successor_run"]["run_id"] == "run-successor-observer"
    assert ticket["lineage"]["successor_of_run_id"] == "run-lost-observer"
    assert ticket["execution_ticket"]["status"] == "issued"
    assert ticket["role_policy"]["canonical_role"] == "observer"
    encoded = json.dumps(ticket, sort_keys=True)
    assert "must-not-appear" not in encoded
    assert '"credential":' not in encoded


def test_l3_lost_run_reports_to_parent_without_authorizing_replacement():
    inputs = _inputs(role="mf_sub", failed_principal="worker-1")
    inputs["failed_run"]["parent_run_id"] = "run-parent-l2"
    inputs["requester_notification"]["target_role"] = "parent_l2"
    inputs["successor_run"] = None
    inputs["successor_launch_identity"] = None

    ticket = build_cli_agent_successor_ticket(**inputs)

    assert ticket["status"] == "reported"
    assert ticket["issue_allowed"] is False
    assert ticket["decision"] == "report_to_parent_l2"
    assert ticket["execution_ticket"] == {}


def test_qa_successor_requires_independent_replacement_principal():
    inputs = _inputs(role="qa", failed_principal="qa-1")
    inputs["successor_run"]["principal_id"] = "qa-1"
    rejected = build_cli_agent_successor_ticket(**inputs)
    assert rejected["status"] == "rejected"
    assert "QA successor requires an independent replacement principal" in rejected[
        "errors"
    ]

    inputs["successor_run"]["principal_id"] = "qa-2"
    issued = build_cli_agent_successor_ticket(**inputs)
    assert issued["status"] == "issued"
    assert issued["role_policy"]["require_independent_principal"] is True


def test_successor_budget_profile_and_authority_fail_closed():
    inputs = _inputs()
    inputs["retry_state"]["successor_count"] = 2
    exhausted = build_cli_agent_successor_ticket(**inputs)
    assert "successor budget is exhausted" in exhausted["errors"]

    inputs = _inputs()
    inputs["role_policy"]["successor_budget"] = 99
    inflated = build_cli_agent_successor_ticket(**inputs)
    assert "requested successor budget does not match ContractRuntime" in inflated[
        "errors"
    ]

    inputs = _inputs()
    inputs["failed_run"]["role"] = "mf_sub"
    role_mismatch = build_cli_agent_successor_ticket(**inputs)
    assert "failed run role does not match ContractRuntime policy" in role_mismatch[
        "errors"
    ]

    inputs = _inputs()
    inputs["successor_run"]["profile_id"] = "other-profile"
    changed_profile = build_cli_agent_successor_ticket(**inputs)
    assert "successor cannot silently change profile" in changed_profile["errors"]

    inputs = _inputs()
    inputs["contract_runtime_current_state"]["source_of_authority"] = "timeline"
    wrong_authority = build_cli_agent_successor_ticket(**inputs)
    assert "successor authority must be ContractRuntime" in wrong_authority["errors"]


def test_persisted_lost_receipt_must_belong_to_the_authority_backlog():
    receipt = _lost_receipt()
    wrong_backlog_event = {
        "id": 12,
        "backlog_id": "AC-OTHER-BACKLOG",
        "payload": {"cli_agent_run_receipt": receipt},
    }
    assert (
        _cli_agent_persisted_receipt_event(
            [wrong_backlog_event],
            backlog_id="AC-CLI-SUCCESSOR",
            receipt_id=receipt["receipt_id"],
        )
        == {}
    )

    matching_event = {
        **wrong_backlog_event,
        "id": 13,
        "backlog_id": "AC-CLI-SUCCESSOR",
    }
    assert _cli_agent_persisted_receipt_event(
        [wrong_backlog_event, matching_event],
        backlog_id="AC-CLI-SUCCESSOR",
        receipt_id=receipt["receipt_id"],
    )["id"] == 13
