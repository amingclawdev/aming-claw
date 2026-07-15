from __future__ import annotations

import hashlib
import json
import os

import pytest

from agent.governance import contract_state_runtime
from agent.governance.contract_state_runtime import (
    _bounded_qa_graph_context_satisfies_requirement,
    _current_full_reconcile_satisfies_requirement,
    build_cli_agent_execution_ticket,
    build_contract_state_projection,
    resolve_cli_agent_observer_admission,
)


def test_resolve_cli_agent_observer_admission_uses_current_contract_authority():
    state = {
        "contract_id": "cli_agent_observer.v1",
        "project_id": "aming-claw",
        "backlog_id": "AC-1",
        "contract_execution_id": "cex-1",
        "execution_state_revision": 3,
        "execution_state_hash": "sha256:state",
        "route_token_ref": "rtok-1",
        "next_legal_action": {"line_id": "observer_service_admission"},
    }
    runtime = {
        "runtime_context_id": "rctx-1",
        "task_id": "task-1",
        "worker_id": "observer-1",
        "worker_slot_id": "observer-1",
        "observer_command_id": "cmd-1",
        "principal_id": "observer-1",
        "expected_dispatch_identity_hash": "sha256:dispatch",
        "route_id": "route-1",
        "route_context_hash": "sha256:route",
        "prompt_contract_id": "prompt-1",
        "prompt_contract_hash": "sha256:prompt",
        "visible_injection_manifest_hash": "sha256:manifest",
    }
    admission = resolve_cli_agent_observer_admission(
        state,
        runtime_identity=runtime,
        profile_requirements={
            "role": "observer",
            "profile_id": "observer",
            "backend_mode": "managed",
        },
    )
    assert admission["authority_selectors"]["contract_execution_id"] == "cex-1"
    assert admission["authority_selectors"]["expected_execution_state_revision"] == 3
    assert admission["source_of_authority"] == "ContractRuntime.next_legal_action"
    assert admission["direct_invocation_fallback"] is False


def test_resolve_cli_agent_observer_admission_rejects_non_current_line():
    with pytest.raises(ValueError, match="not the next legal action"):
        resolve_cli_agent_observer_admission(
            {
                "contract_id": "cli_agent_observer.v1",
                "next_legal_action": {"line_id": "observer_session_active"},
            },
            runtime_identity={},
            profile_requirements={},
        )


def _event(
    event_id: int,
    kind: str,
    *,
    status: str = "passed",
    payload=None,
    verification=None,
    project_id: str = "",
    created_at: str = "",
    commit_sha: str = "",
):
    return {
        "id": event_id,
        "project_id": project_id,
        "backlog_id": "AC-CONTRACT-RUNTIME",
        "event_kind": kind,
        "phase": "contract",
        "status": status,
        "payload": payload or {},
        "verification": verification or {},
        "artifact_refs": {},
        "created_at": created_at,
        "commit_sha": commit_sha,
    }


def _bounded_qa_graph_evidence(*, base_diff: bool = False) -> dict:
    trace_id = "gqt-contract-state-qa"
    evidence = {
        "source": "graph_query_traces",
        "db_verified": True,
        "trace_ids": [trace_id],
        "verified_trace_ids": [trace_id],
        "missing_trace_ids": [],
        "identity_mismatches": [],
        "qa_session_id": "ses-qa-bounded",
        "qa_principal": "qa:bounded",
        "canonical_base_snapshot_id": "full-base",
        "base_commit_sha": "a" * 40,
        "changed_files_source": "server_exact_candidate_snapshot",
        "root_identity_hash": "sha256:" + "e" * 64,
        "query_root_identity_hash": "sha256:" + "f" * 64,
        "canonical_project_identity_hash": "sha256:" + "1" * 64,
        "repository_identity_hash": "sha256:" + "2" * 64,
    }
    if not base_diff:
        evidence.update(
            {
                "graph_basis": "exact_candidate_snapshot",
                "candidate_commit_sha": "a" * 40,
                "changed_files": [],
                "candidate_diff_hash": "sha256:" + hashlib.sha256(b"").hexdigest(),
            }
        )
        return evidence
    evidence.update(
        {
            "graph_basis": "canonical_base_plus_candidate_diff",
            "candidate_commit_sha": "b" * 40,
            "changed_files": ["agent/governance/server.py"],
            "candidate_diff_hash": "sha256:" + "c" * 64,
            "changed_files_source": "server_git_diff_name_status_z_m",
            "candidate_overlay_hash": "sha256:" + "d" * 64,
            "root_identity_hash": "sha256:" + "e" * 64,
            "query_root_identity_hash": "sha256:" + "f" * 64,
            "canonical_project_identity_hash": "sha256:" + "1" * 64,
            "repository_identity_hash": "sha256:" + "2" * 64,
        }
    )
    return evidence


def _cli_agent_ticket_fixture() -> tuple[dict, dict]:
    launch = {
        "project_id": "aming-claw",
        "backlog_id": "AC-CLI-TICKET",
        "task_id": "task-cli-ticket",
        "parent_task_id": "observer-cli-ticket",
        "runtime_context_id": "mfrctx-cli-ticket",
        "worker_role": "mf_sub",
        "target_project_root": "/tmp/aming-claw",
        "worktree_path": "/tmp/cli-ticket-worker",
        "branch_ref": "refs/heads/codex/cli-ticket",
        "base_commit": "a" * 40,
        "target_head_commit": "a" * 40,
        "merge_queue_id": "mq-cli-ticket",
        "owned_files": ["agent/observer_runtime.py"],
        "route_id": "route-cli-ticket",
        "route_context_hash": "sha256:route-cli-ticket",
        "prompt_contract_id": "rprompt-cli-ticket",
        "prompt_contract_hash": "sha256:prompt-cli-ticket",
        "route_token_ref": "rtok-cli-ticket",
        "visible_injection_manifest_hash": "sha256:manifest-cli-ticket",
        "route_token": {"raw": "must-not-appear"},
    }
    current = {
        "schema_version": "contract_runtime_current_state.v1",
        "source_of_authority": "ContractRuntime",
        "authority_decision_source": "contract_runtime_completed_dispatch_line",
        "project_id": "aming-claw",
        "backlog_id": "AC-CLI-TICKET",
        "contract_execution_id": "cex-cli-ticket",
        "contract_revision_id": "rev-cli-ticket",
        "execution_state_revision": 7,
        "execution_state_hash": "sha256:state-cli-ticket",
        "runtime_guide_hash": "sha256:guide-cli-ticket",
        "readiness_state": "contract_active",
        "next_legal_action": {
            "id": "worker_dispatch",
            "action": "dispatch_bounded_worker",
            "runtime_context_id": "mfrctx-cli-ticket",
            "task_id": "task-cli-ticket",
            "parent_task_id": "observer-cli-ticket",
            "worker_role": "mf_sub",
            "target_project_root": "/tmp/aming-claw",
            "worktree_path": "/tmp/cli-ticket-worker",
            "branch_ref": "refs/heads/codex/cli-ticket",
            "base_commit": "a" * 40,
            "target_head_commit": "a" * 40,
            "merge_queue_id": "mq-cli-ticket",
            "owned_files": ["agent/observer_runtime.py"],
            "route_id": "route-cli-ticket",
            "route_context_hash": "sha256:route-cli-ticket",
            "prompt_contract_id": "rprompt-cli-ticket",
            "prompt_contract_hash": "sha256:prompt-cli-ticket",
            "route_token_ref": "rtok-cli-ticket",
            "visible_injection_manifest_hash": "sha256:manifest-cli-ticket",
            "profile_requirements": {
                "profile_id": "codex-current",
                "harness": "codex",
                "provider": "openai",
                "required_capabilities": ["worker", "tool_use"],
            },
            "retry_policy": {
                "attempt": 1,
                "max_attempts": 2,
                "on_quota_failure": "create_successor",
            },
        },
    }
    return current, launch


def _qa_cli_agent_ticket_fixture() -> tuple[dict, dict]:
    current, launch = _cli_agent_ticket_fixture()
    current["authority_decision_source"] = (
        "contract_runtime_qa_execution_ticket"
    )
    current["next_legal_action"].update(
        {
            "worker_role": "qa",
            "profile_requirements": {
                "harness": "codex",
                "provider": "openai",
                "role": "qa",
                "required_capabilities": ["independent_verification"],
            },
        }
    )
    launch["worker_role"] = "qa"
    return current, launch


def test_cli_agent_execution_ticket_is_idempotent_and_public_only():
    current, launch = _cli_agent_ticket_fixture()
    kwargs = {
        "contract_runtime_current_state": current,
        "launch_identity": launch,
        "profile_requirements": {
            "profile_id": "codex-current",
            "harness": "codex",
            "provider": "openai",
            "required_capabilities": ["worker", "tool_use"],
            "api_key": "must-not-appear",
        },
        "retry_policy": {
            "attempt": 1,
            "max_attempts": 2,
            "on_quota_failure": "create_successor",
            "unbounded_retry": True,
        },
        "expected_execution_state_revision": 7,
        "expected_execution_state_hash": "sha256:state-cli-ticket",
    }
    first = build_cli_agent_execution_ticket(**kwargs)
    second = build_cli_agent_execution_ticket(**kwargs)

    assert first == second
    assert first["status"] == "issued"
    assert first["issue_allowed"] is True
    assert first["source_of_authority"] == "ContractRuntime"
    assert first["contract_execution_id"] == "cex-cli-ticket"
    assert first["execution_state_revision"] == 7
    assert first["dispatch_identity"]["runtime_context_id"] == "mfrctx-cli-ticket"
    assert first["dispatch_identity"]["target_project_root"] == "/tmp/aming-claw"
    assert first["dispatch_identity"]["worktree_path"] == "/tmp/cli-ticket-worker"
    assert first["next_legal_action_hash"] == second["next_legal_action_hash"]
    assert first["ticket_hash"] == second["ticket_hash"]
    assert first["profile_requirements"]["profile_id"] == "codex-current"
    encoded = json.dumps(first, sort_keys=True)
    assert "must-not-appear" not in encoded
    assert "api_key" not in encoded
    assert "route_token\"" not in encoded


def test_cli_agent_execution_ticket_rejects_stale_or_mismatched_authority():
    current, launch = _cli_agent_ticket_fixture()
    stale = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
        expected_execution_state_revision=6,
    )
    assert stale["status"] == "rejected"
    assert "stale execution_state_revision" in stale["errors"]

    mismatched_launch = {**launch, "worktree_path": "/tmp/other-worker"}
    mismatched = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=mismatched_launch,
    )
    assert mismatched["status"] == "rejected"
    assert mismatched["mismatches"][0]["field"] == "worktree_path"

    mismatched_root = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity={**launch, "target_project_root": "/tmp/other-root"},
    )
    assert mismatched_root["status"] == "rejected"
    assert any(
        item["field"] == "target_project_root"
        for item in mismatched_root["mismatches"]
    )

    mismatched_branch = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity={**launch, "branch_ref": "refs/heads/codex/other"},
    )
    assert mismatched_branch["status"] == "rejected"
    assert any(
        item["field"] == "branch_ref" for item in mismatched_branch["mismatches"]
    )

    invented_profile = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
        profile_requirements={"profile_id": "another-account", "harness": "codex"},
    )
    assert invented_profile["status"] == "rejected"
    assert "profile requirements do not match current ContractRuntime action" in (
        invented_profile["errors"]
    )

    identity_free = {
        **current,
        "next_legal_action": {
            "id": "worker_dispatch",
            "action": "dispatch_bounded_worker",
            "profile_requirements": current["next_legal_action"][
                "profile_requirements"
            ],
            "retry_policy": current["next_legal_action"]["retry_policy"],
        },
    }
    unbound = build_cli_agent_execution_ticket(
        contract_runtime_current_state=identity_free,
        launch_identity=launch,
    )
    assert unbound["status"] == "rejected"
    assert "next_legal_action.runtime_context_id" in unbound[
        "missing_authority_fields"
    ]


def test_cli_agent_execution_ticket_accepts_equal_root_and_requires_policy_maps():
    current, launch = _cli_agent_ticket_fixture()
    equal_root = "/tmp/equal-cli-ticket"
    current["next_legal_action"]["target_project_root"] = equal_root
    current["next_legal_action"]["worktree_path"] = equal_root
    launch["target_project_root"] = equal_root
    launch["worktree_path"] = equal_root

    issued = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )

    assert issued["status"] == "issued"
    assert issued["dispatch_identity"]["target_project_root"] == equal_root
    assert issued["dispatch_identity"]["worktree_path"] == equal_root

    for field in ("profile_requirements", "retry_policy"):
        incomplete = json.loads(json.dumps(current))
        incomplete["next_legal_action"][field] = {}
        rejected = build_cli_agent_execution_ticket(
            contract_runtime_current_state=incomplete,
            launch_identity=launch,
        )
        assert rejected["status"] == "rejected"
        assert f"next_legal_action.{field}" in rejected[
            "missing_authority_fields"
        ]


def test_cli_agent_execution_ticket_rejects_non_dispatch_decision_source():
    current, launch = _cli_agent_ticket_fixture()
    current["authority_decision_source"] = "contract_runtime_current_state"

    rejected = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )

    assert rejected["status"] == "rejected"
    assert (
        "execution ticket authority must come from accepted ContractRuntime dispatch line"
        in rejected["errors"]
    )


def test_cli_agent_execution_ticket_accepts_qa_owned_contract_runtime_action(
    monkeypatch,
):
    current, launch = _qa_cli_agent_ticket_fixture()

    issued = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )

    assert issued["status"] == "issued"
    assert issued["authority_decision_source"] == (
        "contract_runtime_qa_execution_ticket"
    )
    assert issued["profile_requirements"]["role"] == "qa"
    assert "profile_id" not in issued["profile_requirements"]
    binding = issued["qa_bootstrap_guide_contract"]
    assert binding["schema_version"] == (
        "cli_agent.qa_bootstrap_guide_contract.v1"
    )
    assert binding["guide_version"] == "qa-bootstrap-guide.v6"
    assert binding["guide_hash"].startswith("sha256:")
    guide_contract = contract_state_runtime.cli_agent_qa_bootstrap_guide_contract()
    assert guide_contract["guide_version"] == binding["guide_version"]
    assert guide_contract["guide_hash"] == binding["guide_hash"]
    prompt_template = guide_contract["prompt_template"]
    skill_token = "$aming-claw:aming-claw-onboard"
    onboard_instruction = (
        "Immediately use that skill to call managed MCP "
        "`onboard_route_guide` with exactly project_id={project_id}, "
        "backlog_id={backlog_id}, role=qa, and work_type=qa_verification."
    )
    assert prompt_template.startswith(skill_token + "\n")
    assert prompt_template.count(skill_token) == 1
    assert onboard_instruction in prompt_template
    skill_index = prompt_template.index(skill_token)
    onboard_index = prompt_template.index(onboard_instruction)
    assert skill_index < onboard_index
    for post_onboard_action in (
        "3. In assigned_worktree, run exactly `git rev-parse HEAD`",
        "4. Call qa_session_register",
        "5. Immediately call managed MCP `graph_query`",
        "managed MCP `contract_runtime_current`",
        "run only the refreshed guide's focused exact pytest node ids",
        "or send a final response",
    ):
        assert onboard_index < prompt_template.index(post_onboard_action)
    assert "managed MCP `contract_runtime_current`" in prompt_template
    assert "managed MCP `contract_runtime_guide`" in prompt_template
    assert "compact read-only CLI projections" in prompt_template
    assert "ContractRuntime remains the source of authority" in prompt_template
    for graph_argument in (
        "tool=query_schema",
        "query_source=qa",
        "query_purpose=independent_verification",
        "project_id={project_id}",
        "backlog_id={backlog_id}",
        "task_id={original_worker_task_id}",
        "commit_sha=<full git HEAD>",
        "repo_root={assigned_worktree}",
        "qa_session_token_ref=<opaque ref from step 4>",
    ):
        assert graph_argument in prompt_template
    assert "contract_execution_id and principal_id" in prompt_template
    assert "raw token internal and never returns it" in prompt_template
    assert "qa_session_token=<raw" not in prompt_template
    assert "writer_role_safe_copy_payload.copy_payload unchanged" in prompt_template
    assert "schema_version=mf_parallel.qa_graph_context.v1" in prompt_template
    assert "graph_trace_ids=[<returned trace_id>]" in prompt_template
    assert "graph_query_trace_ids=[<returned trace_id>]" in prompt_template
    assert "focused exact pytest node ids" in prompt_template
    assert "tests list records every exact pytest node id and outcome" in prompt_template
    assert "starts with a clear PASS: or FAIL:" in prompt_template
    assert "execution_state_revision to be strictly greater" in prompt_template
    assert "Before this graph call succeeds, do not read ContractRuntime" in (
        prompt_template
    )
    assert "run tests, or send a final response" in prompt_template
    assert "If graph_query returns an error" in prompt_template
    assert "report only a public blocker" in prompt_template
    assert "Read-only current/guide calls and a process exit 0 are not completion" in (
        prompt_template
    )
    graph_index = prompt_template.index("Immediately call managed MCP `graph_query`")
    for graph_gated_step in (
        "Only after graph_query returns a successful trace_id",
        "Call managed MCP `contract_runtime_submit_line` for qa_graph_context",
        "focused exact pytest node ids",
        "exactly once for qa_independent_verification",
        "Re-read both managed MCP projections with the same",
    ):
        assert graph_index < prompt_template.index(graph_gated_step)
    assert "qa_bootstrap_guide_contract" not in issued["profile_requirements"]
    onboard_binding = issued["qa_onboard_guidance_contract"]
    assert onboard_binding["schema_version"] == (
        "cli_agent.qa_onboard_guidance_contract.v1"
    )
    assert onboard_binding["guidance_version"] == "qa-onboard-guidance.v1"
    assert onboard_binding["guidance_hash"].startswith("sha256:")
    onboard_contract = (
        contract_state_runtime.cli_agent_qa_onboard_guidance_contract()
    )
    assert onboard_contract["guidance_hash"] == onboard_binding["guidance_hash"]
    assert onboard_contract["token_transport"] == {
        "source": "qa_session_register.qa_session_token_ref",
        "transport": "managed_mcp_process_local_opaque_ref_argument",
        "raw_value_exposed": False,
        "persisted": False,
        "safe_ref_evidence_allowed": True,
        "scope_binding": [
            "project_id",
            "backlog_id",
            "task_id",
            "commit_sha",
            "contract_execution_id",
            "session_id",
        ],
    }
    assert [
        step["id"]
        for step in onboard_contract["line_contracts"]["qa_graph_context"][
            "ordered_steps"
        ][:2]
    ] == ["qa_session_register", "graph_query_schema"]
    graph_arguments = onboard_contract["line_contracts"]["qa_graph_context"][
        "ordered_steps"
    ][1]["arguments"]
    assert graph_arguments["qa_session_token_ref"] == "$qa_session_token_ref"
    assert "qa_session_token" not in graph_arguments
    compact_projection = onboard_contract["compact_selected_role_projection"]
    assert compact_projection["projection_version"] == (
        "qa-selected-role-compact.v1"
    )
    assert compact_projection["automatic_selector"] == {
        "role": "qa",
        "work_type": "qa_verification",
        "backlog_required": True,
        "caller_flag_required": False,
    }
    assert compact_projection[
        "mcp_text_content_serialized_char_limit"
    ] == 64000
    assert compact_projection[
        "http_envelope_serialization_reserve_chars"
    ] == 512
    assert "qa_onboard_guidance_contract" not in issued["profile_requirements"]
    tooling_binding = issued["managed_profile_tooling_contract"]
    assert tooling_binding["schema_version"] == (
        "cli_agent.managed_profile_tooling_contract.v1"
    )
    assert tooling_binding["tooling_version"] == "managed-profile-tooling.v1"
    assert tooling_binding["source_payload_digest"].startswith("sha256:")
    assert tooling_binding["tooling_hash"].startswith("sha256:")
    assert "managed_profile_tooling_contract" not in issued[
        "profile_requirements"
    ]

    same = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    assert same["ticket_id"] == issued["ticket_id"]
    assert same["ticket_hash"] == issued["ticket_hash"]
    assert same["qa_bootstrap_guide_contract"] == binding
    assert same["qa_onboard_guidance_contract"] == onboard_binding
    assert same["managed_profile_tooling_contract"] == tooling_binding

    original_bootstrap_version = (
        contract_state_runtime.CLI_AGENT_QA_BOOTSTRAP_GUIDE_VERSION
    )
    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_BOOTSTRAP_GUIDE_VERSION",
        "qa-bootstrap-guide.v5",
    )
    previous_bootstrap = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    assert previous_bootstrap["qa_bootstrap_guide_contract"][
        "guide_version"
    ] == "qa-bootstrap-guide.v5"
    assert previous_bootstrap["qa_bootstrap_guide_contract"] != binding
    assert previous_bootstrap["ticket_id"] != issued["ticket_id"]
    assert previous_bootstrap["ticket_hash"] != issued["ticket_hash"]
    for stable_field in (
        "execution_state_revision",
        "contract_revision_id",
        "dispatch_identity",
        "dispatch_identity_hash",
        "profile_requirements",
        "retry_policy",
        "qa_onboard_guidance_contract",
        "managed_profile_tooling_contract",
    ):
        assert previous_bootstrap[stable_field] == issued[stable_field]
    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_BOOTSTRAP_GUIDE_VERSION",
        original_bootstrap_version,
    )
    deterministic_v6 = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    assert deterministic_v6["ticket_id"] == issued["ticket_id"]
    assert deterministic_v6["ticket_hash"] == issued["ticket_hash"]
    assert deterministic_v6["qa_bootstrap_guide_contract"] == binding

    original_onboard_version = (
        contract_state_runtime.CLI_AGENT_QA_ONBOARD_GUIDANCE_VERSION
    )
    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_ONBOARD_GUIDANCE_VERSION",
        original_onboard_version + ".changed",
    )
    onboard_changed = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    assert onboard_changed["status"] == "issued"
    assert onboard_changed["execution_state_revision"] == issued[
        "execution_state_revision"
    ]
    assert onboard_changed["contract_revision_id"] == issued[
        "contract_revision_id"
    ]
    assert onboard_changed["dispatch_identity"] == issued["dispatch_identity"]
    assert onboard_changed["dispatch_identity_hash"] == issued[
        "dispatch_identity_hash"
    ]
    assert onboard_changed["profile_requirements"] == issued["profile_requirements"]
    assert onboard_changed["retry_policy"] == issued["retry_policy"]
    assert onboard_changed["qa_bootstrap_guide_contract"] == binding
    assert onboard_changed["managed_profile_tooling_contract"] == tooling_binding
    assert onboard_changed["qa_onboard_guidance_contract"] != onboard_binding
    assert onboard_changed["ticket_id"] != issued["ticket_id"]
    assert onboard_changed["ticket_hash"] != issued["ticket_hash"]
    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_ONBOARD_GUIDANCE_VERSION",
        original_onboard_version,
    )

    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_MANAGED_PROFILE_TOOLING_VERSION",
        contract_state_runtime.CLI_AGENT_MANAGED_PROFILE_TOOLING_VERSION
        + ".changed",
    )
    tooling_changed = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )

    assert tooling_changed["status"] == "issued"
    assert tooling_changed["execution_state_revision"] == issued[
        "execution_state_revision"
    ]
    assert tooling_changed["dispatch_identity_hash"] == issued[
        "dispatch_identity_hash"
    ]
    assert tooling_changed["profile_requirements"] == issued[
        "profile_requirements"
    ]
    assert tooling_changed["retry_policy"] == issued["retry_policy"]
    assert tooling_changed["managed_profile_tooling_contract"] != tooling_binding
    assert tooling_changed["ticket_id"] != issued["ticket_id"]
    assert tooling_changed["ticket_hash"] != issued["ticket_hash"]

    monkeypatch.setattr(
        contract_state_runtime,
        "CLI_AGENT_QA_BOOTSTRAP_GUIDE_PROMPT_TEMPLATE",
        contract_state_runtime.CLI_AGENT_QA_BOOTSTRAP_GUIDE_PROMPT_TEMPLATE
        + "\nVersioned guide change.",
    )
    changed = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )

    assert changed["status"] == "issued"
    assert changed["execution_state_revision"] == tooling_changed[
        "execution_state_revision"
    ]
    assert changed["dispatch_identity_hash"] == tooling_changed[
        "dispatch_identity_hash"
    ]
    assert changed["profile_requirements"] == tooling_changed[
        "profile_requirements"
    ]
    assert changed["retry_policy"] == tooling_changed["retry_policy"]
    assert changed["qa_bootstrap_guide_contract"] != tooling_changed[
        "qa_bootstrap_guide_contract"
    ]
    assert changed["dispatch_identity"] == tooling_changed["dispatch_identity"]
    assert changed["ticket_id"] != tooling_changed["ticket_id"]
    assert changed["ticket_hash"] != tooling_changed["ticket_hash"]


def test_cli_agent_qa_ticket_rotates_for_each_executable_guidance_semantic(
    monkeypatch,
):
    current, launch = _qa_cli_agent_ticket_fixture()
    baseline_machine_contract = json.loads(
        json.dumps(
            contract_state_runtime.CLI_AGENT_QA_ONBOARD_GUIDANCE_MACHINE_CONTRACT
        )
    )
    baseline = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    assert baseline["status"] == "issued"

    mutations = [
        (
            (
                "line_contracts", "qa_graph_context", "ordered_steps", 3,
                "copy_all_safe_fields",
            ),
            False,
        ),
        (
            (
                "line_contracts", "qa_graph_context", "ordered_steps", 3,
                "copy_payload_source",
            ),
            "changed.writer_role_safe_copy_payload.copy_payload",
        ),
        (
            ("token_transport", "transport"),
            "changed-token-transport",
        ),
        (
            (
                "line_contracts", "qa_graph_context", "ordered_steps", 1,
                "blocked_before_success",
            ),
            ["changed-graph-gate"],
        ),
        (
            (
                "line_contracts", "qa_independent_verification",
                "ordered_steps", 3, "add_arguments", "payload", "summary",
            ),
            "<changed verdict payload summary>",
        ),
        (
            (
                "compact_selected_role_projection",
                "projection_version",
            ),
            "qa-selected-role-compact.changed",
        ),
    ]
    authority_fields = (
        "execution_state_revision",
        "contract_revision_id",
        "dispatch_identity",
        "dispatch_identity_hash",
        "profile_requirements",
        "retry_policy",
        "qa_bootstrap_guide_contract",
        "managed_profile_tooling_contract",
    )

    for path, changed_value in mutations:
        candidate_contract = json.loads(json.dumps(baseline_machine_contract))
        cursor = candidate_contract
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = changed_value
        monkeypatch.setattr(
            contract_state_runtime,
            "CLI_AGENT_QA_ONBOARD_GUIDANCE_MACHINE_CONTRACT",
            candidate_contract,
        )

        changed = build_cli_agent_execution_ticket(
            contract_runtime_current_state=current,
            launch_identity=launch,
        )

        assert changed["status"] == "issued"
        assert changed["qa_onboard_guidance_contract"]["guidance_hash"] != (
            baseline["qa_onboard_guidance_contract"]["guidance_hash"]
        )
        assert changed["ticket_id"] != baseline["ticket_id"]
        assert changed["ticket_hash"] != baseline["ticket_hash"]
        for field in authority_fields:
            assert changed[field] == baseline[field]


def test_cli_agent_qa_ticket_id_changes_with_source_payload_digest(
    tmp_path,
    monkeypatch,
):
    current, launch = _cli_agent_ticket_fixture()
    current["authority_decision_source"] = "contract_runtime_qa_execution_ticket"
    current["next_legal_action"].update(
        {
            "worker_role": "qa",
            "profile_requirements": {
                "harness": "codex",
                "provider": "openai",
                "role": "qa",
                "required_capabilities": ["independent_verification"],
            },
        }
    )
    launch["worker_role"] = "qa"
    source_root = tmp_path / "plugin-source"
    source_root.mkdir()
    payload = source_root / "payload.txt"
    payload.write_bytes(b"first source payload")
    digest_source = (
        contract_state_runtime.cli_agent_managed_profile_source_payload_digest
    )
    monkeypatch.setattr(
        contract_state_runtime,
        "CODEX_PLUGIN_PAYLOAD",
        ("payload.txt",),
    )
    monkeypatch.setattr(
        contract_state_runtime,
        "cli_agent_managed_profile_source_payload_digest",
        lambda _plugin_source_root=None: digest_source(source_root),
    )

    first = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    payload.write_bytes(b"second source payload")
    changed = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )

    assert first["status"] == changed["status"] == "issued"
    assert first["execution_state_revision"] == changed[
        "execution_state_revision"
    ]
    assert first["dispatch_identity_hash"] == changed[
        "dispatch_identity_hash"
    ]
    assert first["profile_requirements"] == changed["profile_requirements"]
    assert first["managed_profile_tooling_contract"][
        "tooling_version"
    ] == changed["managed_profile_tooling_contract"]["tooling_version"]
    assert first["managed_profile_tooling_contract"][
        "source_payload_digest"
    ] != changed["managed_profile_tooling_contract"]["source_payload_digest"]
    assert first["ticket_id"] != changed["ticket_id"]
    assert first["ticket_hash"] != changed["ticket_hash"]


@pytest.mark.parametrize(
    "missing_path",
    ("payload", "payload/required.txt"),
)
def test_managed_profile_source_digest_rejects_missing_canonical_payload(
    tmp_path,
    monkeypatch,
    missing_path,
):
    source_root = tmp_path / "plugin-source"
    source_root.mkdir()
    if missing_path != "payload":
        (source_root / "payload").mkdir()
    monkeypatch.setattr(
        contract_state_runtime,
        "CODEX_PLUGIN_PAYLOAD",
        ("payload",),
    )
    monkeypatch.setattr(
        contract_state_runtime,
        "REQUIRED_PLUGIN_FILES",
        ("payload/required.txt",),
    )

    with pytest.raises(
        ValueError,
        match=r"payload is missing: {}$".format(missing_path),
    ):
        contract_state_runtime.cli_agent_managed_profile_source_payload_digest(
            source_root
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
@pytest.mark.parametrize("fifo_location", ("top-level", "descendant"))
def test_managed_profile_source_digest_rejects_fifo_without_blocking(
    tmp_path,
    monkeypatch,
    fifo_location,
):
    source_root = tmp_path / "plugin-source"
    source_root.mkdir()
    if fifo_location == "top-level":
        fifo = source_root / "payload"
        payload = ("payload",)
        expected_path = "payload"
    else:
        payload_root = source_root / "payload"
        payload_root.mkdir()
        fifo = payload_root / "pipe"
        payload = ("payload",)
        expected_path = "payload/pipe"
    monkeypatch.setattr(contract_state_runtime, "CODEX_PLUGIN_PAYLOAD", payload)
    monkeypatch.setattr(contract_state_runtime, "REQUIRED_PLUGIN_FILES", ())
    os.mkfifo(fifo)
    try:
        with pytest.raises(
            ValueError,
            match=r"unsupported file type: {}$".format(expected_path),
        ):
            contract_state_runtime.cli_agent_managed_profile_source_payload_digest(
                source_root
            )
    finally:
        fifo.unlink(missing_ok=True)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is unavailable")
@pytest.mark.parametrize("symlink_location", ("top-level", "descendant"))
def test_managed_profile_source_digest_rejects_symlink(
    tmp_path,
    monkeypatch,
    symlink_location,
):
    source_root = tmp_path / "plugin-source"
    source_root.mkdir()
    target = source_root / "target.txt"
    target.write_bytes(b"symlink target")
    if symlink_location == "top-level":
        link = source_root / "payload"
        expected_path = "payload"
    else:
        payload_root = source_root / "payload"
        payload_root.mkdir()
        link = payload_root / "link"
        expected_path = "payload/link"
    link.symlink_to(target)
    monkeypatch.setattr(
        contract_state_runtime,
        "CODEX_PLUGIN_PAYLOAD",
        ("payload",),
    )
    monkeypatch.setattr(contract_state_runtime, "REQUIRED_PLUGIN_FILES", ())

    with pytest.raises(
        ValueError,
        match=r"contains symlink: {}$".format(expected_path),
    ):
        contract_state_runtime.cli_agent_managed_profile_source_payload_digest(
            source_root
        )


def test_cli_agent_execution_ticket_rejects_consumed_dispatch_identity():
    current, launch = _cli_agent_ticket_fixture()
    issued = build_cli_agent_execution_ticket(
        contract_runtime_current_state=current,
        launch_identity=launch,
    )
    consumed_current = {
        **current,
        "consumed_dispatch_identity_hashes": [issued["dispatch_identity_hash"]],
    }
    rejected = build_cli_agent_execution_ticket(
        contract_runtime_current_state=consumed_current,
        launch_identity=launch,
    )

    assert rejected["status"] == "rejected"
    assert "dispatch identity was already consumed" in rejected["errors"]

    consumed_by_id = {
        **current,
        "consumed_ticket_ids": [issued["ticket_id"]],
    }
    rejected_by_id = build_cli_agent_execution_ticket(
        contract_runtime_current_state=consumed_by_id,
        launch_identity=launch,
    )
    assert "execution ticket id was already consumed" in rejected_by_id["errors"]

    consumed_by_hash = {
        **current,
        "consumed_ticket_hashes": [issued["ticket_hash"]],
    }
    rejected_by_hash = build_cli_agent_execution_ticket(
        contract_runtime_current_state=consumed_by_hash,
        launch_identity=launch,
    )
    assert "execution ticket hash was already consumed" in rejected_by_hash["errors"]


def test_cli_agent_run_receipts_project_operational_state_without_evidence_credit():
    from agent.cli_agent_service.evidence import CliAgentRunReceipt

    identity = {
        "run_id": "run-contract-projection-a",
        "ticket_id": "caet-1234567890abcdef12345678",
        "ticket_hash": "sha256:" + hashlib.sha256(b"ticket").hexdigest(),
        "profile_id": "codex-profile-a",
        "runtime_context_id": "mfrctx-contract-projection-a",
        "command_hash": "sha256:" + hashlib.sha256(b"command").hexdigest(),
    }
    accepted = CliAgentRunReceipt(
        **identity,
        state="accepted",
        event_index=0,
        observed_at="2026-07-12T12:00:00Z",
    ).to_public_dict()
    started = CliAgentRunReceipt(
        **identity,
        state="started",
        event_index=1,
        observed_at="2026-07-12T12:00:01Z",
        process_identity={
            "pid": 1234,
            "process_group_id": 1234,
            "process_start_identity_hash": "sha256:"
            + hashlib.sha256(b"pid:1234:start:99").hexdigest(),
        },
    ).to_public_dict()
    events = [
        _event(
            1,
            "worker_progress",
            status="recorded",
            payload={"cli_agent_run_receipt": accepted},
        ),
        _event(
            2,
            "worker_progress",
            status="recorded",
            payload={"cli_agent_run_receipt": started},
        ),
    ]

    projection = build_contract_state_projection(
        events,
        contract={"required_evidence": ["implementation"]},
        backlog_row={"bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert len(projection["cli_agent_run_receipts"]) == 2
    assert projection["cli_agent_run_state"]["runs"][0]["state"] == "started"
    assert projection["cli_agent_run_state"]["governance_authority"] is False
    assert projection["completed_evidence"] == []
    assert projection["missing_evidence"] == ["implementation"]


def _strict_mf_parallel_contract() -> dict:
    return {
        "contract": {
            "contract_id": "mf_parallel.v2",
            "contract_template_id": "mf_parallel.v2",
            "contract_revision_id": "rev-mf-v2-strict",
            "state": "bound",
            "server_derived_evidence_policies": {
                "bounded_qa_review_policy": {
                    "enabled": True,
                    "line_ids": ["qa_graph_context"],
                },
                "current_full_reconcile_evidence_policy": {
                    "enabled": True,
                    "line_ids": ["observer_reconcile"],
                },
            },
        }
    }


def test_projection_keeps_generated_demo_requirements_conditional():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-1",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "conditional_required_evidence": [
                {
                    "condition": {"target_kind": "generated_demo"},
                    "evidence": ["demo_target_identity_check"],
                }
            ],
        }
    }

    regular = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    demo = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "target_kind": "generated_demo",
        },
    )

    assert regular["required_evidence"] == ["graph_query_schema_trace"]
    assert regular["conditional_required_evidence"][0]["active"] is False
    assert "demo_target_identity_check" not in regular["missing_evidence"]
    assert demo["conditional_required_evidence"][0]["active"] is True
    assert "demo_target_identity_check" in demo["required_evidence"]
    assert demo["next_legal_action"]["id"] == "graph_query_schema_trace"

    trigger_demo = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "chain_trigger_json": json.dumps({"target_kind": "generated_demo"}),
        },
    )

    assert trigger_demo["conditional_required_evidence"][0]["active"] is True
    assert "demo_target_identity_check" in trigger_demo["required_evidence"]


def test_next_action_append_hints_are_meta_contract_appendable():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-hint",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert hint["event_kind"] == "contract_state_changed"
    assert hint["satisfies_by"] == "payload.requirement_id"
    assert hint["payload"]["requirement_id"] == "graph_query_schema_trace"
    assert hint["actor_role"] == "observer"
    assert hint["meta_contract_gate"]["allowed"] is True


def test_independent_verification_lane_hint_is_qa_owned_and_appendable():
    contract = {
        "contract": {
            "contract_id": "mf_workflow_runtime.v1",
            "contract_template_id": "mf_workflow_runtime.v1",
            "contract_revision_id": "rev-qa-lane",
            "state": "selected",
            "evidence_requirements": [
                {
                    "id": "independent_verification_lane",
                    "phase": "verification",
                    "kind": "qa",
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    assert action["id"] == "independent_verification_lane"
    assert hint["event_kind"] == "independent_verification"
    assert hint["actor_role"] == "qa"
    assert hint["meta_contract_gate"]["allowed"] is True
    assert hint["satisfies_by"] == "event_kind"
    assert hint["payload"]["requirement_id"] == "independent_verification_lane"
    assert hint["payload"]["requirement_ids"] == ["independent_verification_lane"]
    assert "qa_review" in hint["accepted_event_kinds"]
    prefill = hint["role_bound_prefill_policy"]
    assert prefill["execution_owner_role"] == "qa"
    assert prefill["observer_owned"] is False
    assert prefill["actor_must_supply_evidence"] is True
    assert "contract_execution_id" in prefill["observer_prefill_fields"]
    assert "verification" in prefill["actor_owned_execution_fields"]

    completed = build_contract_state_projection(
        [_event(12, "qa_review")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert [item["id"] for item in completed["completed_evidence"]] == [
        "independent_verification_lane"
    ]
    assert completed["missing_evidence"] == []


def test_parallel_root_qa_hint_keeps_parent_route_identity_after_child_worker_events():
    root_route = {
        "route_id": "route-root",
        "route_context_hash": "sha256:root-context",
        "prompt_contract_id": "rprompt-root",
        "prompt_contract_hash": "sha256:root-prompt",
        "visible_injection_manifest_hash": "sha256:root-visible",
        "route_token_ref": "rtok-root",
    }
    child_route = {
        "route_id": "route-child",
        "route_context_hash": "sha256:child-context",
        "prompt_contract_id": "rprompt-child",
        "prompt_contract_hash": "sha256:child-prompt",
        "visible_injection_manifest_hash": "sha256:child-visible",
        "route_token_ref": "rtok-child",
    }
    repair_route = {
        "route_id": "event.route_action.pre_mutation",
        "route_context_hash": "sha256:repair-context",
        "prompt_contract_id": "rprompt-repair",
        "prompt_contract_hash": "sha256:repair-prompt",
        "visible_injection_manifest_hash": "sha256:repair-visible",
        "route_token_ref": "rtok-repair",
    }
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_execution_id": "cex-parallel-root",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "route_action_precheck",
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "mf_subagent_finish_gate",
                "independent_verification",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(1, "route_context", payload={"route_identity": root_route}),
            _event(2, "route_action_precheck", payload=root_route),
            _event(3, "bounded_implementation_worker_dispatch", payload=root_route),
            _event(4, "mf_subagent_startup", payload=root_route),
            _event(
                5,
                "route_token_gate",
                payload={
                    **child_route,
                    "route_token_gate": {
                        **child_route,
                        "parent_route_lineage": root_route,
                    }
                },
                verification=child_route,
            ),
            _event(
                6,
                "implementation",
                payload={**child_route, "parent_route_lineage": root_route},
            ),
            _event(
                7,
                "mf_subagent_finish_gate",
                payload={
                    "mf_subagent_finish_gate": {
                        "close_ready": True,
                        "receipt_gate": {
                            "status": "passed",
                            "read_receipt_present": True,
                            "startup_present": True,
                        },
                    },
                    **child_route,
                },
            ),
            _event(
                8,
                "independent_verification",
                status="failed",
                payload={
                    **root_route,
                    "route_identity": root_route,
                    "route_action_scope_lineage": {
                        "child_route_identity": root_route,
                        "child_route_lineage": root_route,
                        "parent_route_lineage": repair_route,
                    },
                    "parent_route_lineage": repair_route,
                },
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "AC-CONTRACT-RUNTIME",
        },
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    assert action["id"] == "independent_verification"
    assert action["route_token_ref"] == "rtok-root"
    assert hint["actor_role"] == "qa"
    assert hint["route_identity"] == root_route
    assert hint["payload"]["route_identity"] == root_route
    assert hint["payload"]["route_token_ref"] == "rtok-root"
    assert projection["active_contract_execution"]["route_token_ref"] == "rtok-root"
    assert projection["runtime_contract_hints"]["route_identity"] == root_route


def test_focused_verification_hint_uses_appendable_qa_event_kind():
    contract = {
        "contract": {
            "contract_id": "observer_hotfix_direct_mutation.v1",
            "contract_template_id": "observer_hotfix_direct_mutation.v1",
            "contract_execution_id": "cex-hotfix",
            "state": "selected",
            "evidence_requirements": [
                {
                    "id": "focused_verification",
                    "event_kind": "verification",
                    "accepted_event_kinds": ["verification", "qa_verification"],
                    "match_policy": "canonical_event_kind",
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert projection["next_legal_action"]["id"] == "focused_verification"
    assert hint["event_kind"] == "qa_verification"
    assert hint["actor_role"] == "qa"
    assert hint["meta_contract_gate"]["allowed"] is True
    assert "verification" in hint["accepted_event_kinds"]


def test_work_mode_transition_hint_carries_route_identity_and_precheck_ref():
    route_identity = {
        "route_id": "route-1",
        "route_context_hash": "sha256:ctx",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt",
        "visible_injection_manifest_hash": "sha256:visible",
        "route_token_ref": "rtok-1",
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-work-mode",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "route_action_precheck",
                "observer_work_mode_transition",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(1, "route_context", payload={"route_context": route_identity}),
            _event(
                2,
                "route_action_precheck",
                payload={"route_action_precheck": route_identity},
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "observer-task-1",
        },
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert projection["next_legal_action"]["id"] == "observer_work_mode_transition"
    assert hint["event_kind"] == "observer_work_mode_transition"
    assert hint["route_identity"] == route_identity
    assert hint["payload"]["route_identity"] == route_identity
    assert hint["payload"]["task_id"] == "observer-task-1"
    assert hint["payload"]["route_action_precheck_ref"]["event_id"] == 2
    assert hint["meta_contract_gate"]["allowed"] is True


def test_work_mode_transition_hint_is_writable_before_route_action_precheck():
    route_identity = {
        "route_id": "route-1",
        "route_context_hash": "sha256:ctx",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt",
        "visible_injection_manifest_hash": "sha256:visible",
        "route_token_ref": "rtok-1",
    }
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-work-mode-before-precheck",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "observer_work_mode_transition",
                "route_action_precheck",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                1,
                "route_context",
                payload={
                    "route_identity": route_identity,
                    **route_identity,
                },
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "observer-task-1",
        },
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert projection["next_legal_action"]["id"] == "observer_work_mode_transition"
    assert hint["event_kind"] == "observer_work_mode_transition"
    assert hint["route_identity"] == route_identity
    assert hint["payload"]["route_identity"] == route_identity
    assert hint["payload"]["task_id"] == "observer-task-1"
    assert "route_action_precheck_ref" not in hint["payload"]
    assert hint["meta_contract_gate"]["allowed"] is True


def test_projection_computes_completed_missing_and_next_action():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-2",
            "state": "selected",
            "required_evidence": ["route_context", "route_action_precheck"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                41,
                "route_context",
                payload={
                    "route_context": {
                        "route_id": "route-1",
                        "route_context_hash": "sha256:ctx",
                        "prompt_contract_id": "rprompt-1",
                        "prompt_contract_hash": "sha256:prompt",
                        "visible_injection_manifest_hash": "sha256:visible",
                    }
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert [item["id"] for item in projection["completed_evidence"]] == ["route_context"]
    assert projection["missing_evidence"] == ["route_action_precheck"]
    assert projection["blocked_evidence"] == []
    assert projection["ordered_next_steps"][0]["id"] == "route_action_precheck"
    assert projection["next_legal_action"]["id"] == "route_action_precheck"
    assert projection["next_legal_action"]["source"] == "contract_state"
    assert projection["next_legal_action"]["precedence"] == "active_contract_missing_step"
    assert projection["next_legal_action"]["contract_execution_id"] == projection[
        "active_contract_execution"
    ]["contract_execution_id"]
    assert projection["next_legal_action"]["contract_chain_id"] == projection[
        "contract_chain_id"
    ]


def test_close_gate_projection_events_complete_hotfix_contract_state():
    contract = {
        "contract": {
            "contract_id": "observer_hotfix_direct_mutation.v1",
            "contract_template_id": "observer_hotfix_direct_mutation.v1",
            "contract_revision_id": "rev-close-projection",
            "state": "selected",
            "required_evidence": [
                "route_context",
                "route_action_precheck",
                "implementation",
                "verification",
                "close_ready",
            ],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(1, "route_context", status="requested"),
            _event(
                2,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.prompt_alert_bundle",
                    "contract_evidence": [
                        {"requirement_id": "route_context_hash"},
                        {"requirement_id": "prompt_contract_hash"},
                        {"requirement_id": "visible_injection_manifest"},
                    ],
                },
            ),
            _event(3, "route_action_precheck", status="requested"),
            _event(
                4,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.action_precheck",
                    "contract_evidence": [
                        {"requirement_id": "route_context_hash"},
                        {"requirement_id": "prompt_contract_id"},
                        {"requirement_id": "prompt_contract_hash"},
                        {"requirement_id": "route_action_allowed"},
                    ],
                },
            ),
            _event(
                5,
                "hotfix_under_action",
                status="accepted",
                payload={
                    "implementation_close_evidence": {
                        "counts_as_implementation": True,
                        "changed_files": ["agent/governance/contract_state_runtime.py"],
                    }
                },
            ),
            _event(
                6,
                "independent_verification",
                status="passed",
                payload={"reviewer_role": "independent_qa"},
            ),
            _event(7, "close_ready", status="accepted"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    completed = {item["id"]: item for item in projection["completed_evidence"]}
    assert completed["route_context"]["event_id"] == 2
    assert completed["route_action_precheck"]["event_id"] == 4
    assert completed["implementation"]["event_id"] == 5
    assert completed["verification"]["event_id"] == 6
    assert completed["close_ready"]["event_id"] == 7
    assert projection["missing_evidence"] == []
    assert projection["contract_complete"] is True
    assert (
        projection["next_legal_action"]["id"]
        == "issue_backlog_close_route_token_then_backlog_close"
    )


def test_legacy_direct_hotfix_followup_skips_worker_only_default_requirements():
    default_required = [
        "route_context",
        "route_action_precheck",
        "bounded_implementation_worker_dispatch",
        "mf_subagent_startup",
        "independent_verification_lane",
        "implementation",
        "verification",
        "close_ready",
    ]

    projection = build_contract_state_projection(
        [
            _event(1, "hotfix_entered", status="accepted"),
            _event(
                2,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.prompt_alert_bundle",
                    "contract_evidence": [
                        {"requirement_id": "route_context_hash"},
                        {"requirement_id": "prompt_contract_hash"},
                    ],
                },
            ),
            _event(
                3,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.action_precheck",
                    "contract_evidence": [
                        {"requirement_id": "route_action_allowed"},
                    ],
                },
            ),
            _event(
                4,
                "hotfix_under_action",
                status="accepted",
                payload={
                    "implementation_close_evidence": {
                        "counts_as_implementation": True,
                        "changed_files": ["agent/governance/contract_state_runtime.py"],
                    }
                },
            ),
        ],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=default_required,
    )

    assert "bounded_implementation_worker_dispatch" not in projection["required_evidence"]
    assert "mf_subagent_startup" not in projection["required_evidence"]
    assert "bounded_implementation_worker_dispatch" not in projection["missing_evidence"]
    assert "mf_subagent_startup" not in projection["missing_evidence"]
    assert projection["missing_evidence"] == [
        "independent_verification_lane",
        "verification",
        "close_ready",
    ]
    assert projection["next_legal_action"]["id"] == "independent_verification_lane"
    assert (
        projection["next_legal_action"]["precedence"]
        == "legacy_hotfix_direct_followup"
    )

    qa_projection = build_contract_state_projection(
        [
            _event(1, "hotfix_entered", status="accepted"),
            _event(
                2,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.prompt_alert_bundle",
                    "contract_evidence": [
                        {"requirement_id": "route_context_hash"},
                        {"requirement_id": "prompt_contract_hash"},
                    ],
                },
            ),
            _event(
                3,
                "service_route",
                status="allowed",
                payload={
                    "service_id": "route.action_precheck",
                    "contract_evidence": [
                        {"requirement_id": "route_action_allowed"},
                    ],
                },
            ),
            _event(
                4,
                "hotfix_under_action",
                status="accepted",
                payload={
                    "implementation_close_evidence": {
                        "counts_as_implementation": True,
                        "changed_files": ["agent/governance/contract_state_runtime.py"],
                    }
                },
            ),
            _event(5, "qa_verification", status="accepted"),
        ],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=default_required,
    )

    assert qa_projection["missing_evidence"] == ["close_ready"]
    assert qa_projection["next_legal_action"]["id"] == "close_ready"
    assert (
        qa_projection["next_legal_action"]["precedence"]
        == "legacy_hotfix_direct_followup"
    )


def test_legacy_direct_hotfix_requires_qa_after_latest_implementation():
    default_required = [
        "route_context",
        "route_action_precheck",
        "bounded_implementation_worker_dispatch",
        "mf_subagent_startup",
        "independent_verification_lane",
        "implementation",
        "verification",
        "close_ready",
    ]
    events = [
        _event(1, "hotfix_entered", status="accepted"),
        _event(
            2,
            "service_route",
            status="allowed",
            payload={
                "service_id": "route.prompt_alert_bundle",
                "contract_evidence": [
                    {"requirement_id": "route_context_hash"},
                    {"requirement_id": "prompt_contract_hash"},
                ],
            },
        ),
        _event(
            3,
            "service_route",
            status="allowed",
            payload={
                "service_id": "route.action_precheck",
                "contract_evidence": [
                    {"requirement_id": "route_action_allowed"},
                ],
            },
        ),
        _event(
            4,
            "hotfix_under_action",
            status="accepted",
            payload={
                "implementation_close_evidence": {
                    "counts_as_implementation": True,
                    "changed_files": ["agent/governance/contract_state_runtime.py"],
                }
            },
        ),
        _event(5, "qa_verification", status="accepted"),
        _event(
            6,
            "hotfix_under_action",
            status="accepted",
            payload={
                "implementation_close_evidence": {
                    "counts_as_implementation": True,
                    "changed_files": ["agent/governance/task_timeline.py"],
                }
            },
        ),
    ]

    stale_qa_projection = build_contract_state_projection(
        events,
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=default_required,
    )

    assert stale_qa_projection["next_legal_action"]["id"] == "independent_verification_lane"
    assert "verification" in stale_qa_projection["missing_evidence"]
    completed = {
        item["id"]: item
        for item in stale_qa_projection["completed_evidence"]
    }
    assert completed["implementation"]["event_id"] == 6
    assert "independent_verification_lane" not in completed
    assert "verification" not in completed

    fresh_qa_projection = build_contract_state_projection(
        [*events, _event(7, "qa_verification", status="accepted")],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=default_required,
    )

    assert fresh_qa_projection["missing_evidence"] == ["close_ready"]
    fresh_completed = {
        item["id"]: item
        for item in fresh_qa_projection["completed_evidence"]
    }
    assert fresh_completed["independent_verification_lane"]["event_id"] == 7
    assert fresh_completed["verification"]["event_id"] == 7
    assert fresh_completed["verification"]["fresh_after_event_id"] == 6


def test_route_requirements_ignore_generic_requirement_id_batches():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-route-direct",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "route_context",
                    "accepted_event_kinds": ["route_context"],
                    "match_policy": "canonical_event_kind",
                },
                {
                    "id": "route_action_precheck",
                    "accepted_event_kinds": ["route_action_precheck"],
                    "match_policy": "canonical_event_kind",
                },
            ],
        }
    }

    generic = build_contract_state_projection(
        [
            _event(
                51,
                "contract_state_changed",
                payload={
                    "requirement_ids": [
                        "route_context",
                        "route_action_precheck",
                    ]
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    canonical = build_contract_state_projection(
        [
            _event(52, "route_context"),
            _event(53, "route_action_precheck"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert generic["completed_evidence"] == []
    assert generic["missing_evidence"] == [
        "route_context",
        "route_action_precheck",
    ]
    assert [item["id"] for item in canonical["completed_evidence"]] == [
        "route_context",
        "route_action_precheck",
    ]
    assert canonical["missing_evidence"] == []


def test_projection_keeps_no_contract_rows_without_chain_requirement():
    projection = build_contract_state_projection(
        [],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["legacy_no_contract"] is True
    assert projection["contract_chain_id"] == ""
    assert projection["contract_chain"] == []
    assert projection["successor_contract_candidates"] == []
    assert projection["selected_successor_contract"] == {}
    assert projection["next_legal_action"]["id"] == "select_or_enter_contract"
    assert projection["next_legal_action"]["precedence"] == "contract_first_pre_mutation"
    assert "observer_hotfix_direct_mutation.v1" in projection["next_legal_action"][
        "candidate_contract_templates"
    ]
    assert "merge" in projection["next_legal_action"]["supported_contract_roles"]
    assert projection["runtime_contract_hints"]["next_legal_operation"][
        "id"
    ] == "select_or_enter_contract"


def test_no_contract_hotfix_entry_prompts_hotfix_under_action():
    projection = build_contract_state_projection(
        [_event(41, "hotfix_entered")],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    action = projection["next_legal_action"]
    assert projection["legacy_no_contract"] is True
    assert action["id"] == "hotfix_post_action_summary"
    assert action["action"] == "record_hotfix_under_action"
    assert action["timeline_append_hint"]["event_kind"] == "hotfix_under_action"
    assert action["timeline_append_hint"]["payload"]["pre_reason_event_id"] == "41"
    implementation_close_evidence = action["timeline_append_hint"]["payload"][
        "implementation_close_evidence"
    ]
    assert implementation_close_evidence["counts_as_implementation"] is True
    assert implementation_close_evidence["changed_files"] == []
    assert implementation_close_evidence["verification_evidence_refs"] == []
    assert implementation_close_evidence["qa_lineage"]["required"] is True
    assert implementation_close_evidence["qa_lineage"]["required_gate"] == (
        "independent_qa_gate"
    )


def test_root_terminal_contract_with_close_ready_prompts_backlog_close_route_token():
    route_identity = {
        "route_id": "route-root",
        "route_context_hash": "sha256:ctx-root",
        "prompt_contract_id": "rprompt-root",
        "prompt_contract_hash": "sha256:prompt-root",
        "visible_injection_manifest_hash": "sha256:visible-root",
        "route_token_ref": "rtok-parent",
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-root-terminal",
            "contract_execution_id": "cex-root-terminal",
            "contract_revision_id": "rev-root-terminal",
            "state": "selected",
            "route_identity": route_identity,
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(21, "route_context", payload={"route_context": route_identity}),
            _event(
                22,
                "close_ready",
                status="accepted",
                payload={"contract_execution_id": "cex-root-terminal"},
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "task-root-terminal",
            "target_files": json.dumps(["agent/governance/contract_state_runtime.py"]),
        },
    )

    action = projection["next_legal_action"]
    assert action["id"] == "issue_backlog_close_route_token_then_backlog_close"
    assert action["action"] == "backlog_close"
    assert action["allowed_action"] == "backlog_close"
    assert action["close_ready_evidence_refs"][0]["ref"] == "timeline:22"
    hint = action["route_token_request_hint"]
    assert hint["allowed_actions"] == ["backlog_close"]
    assert hint["issue_route_token_request"]["allowed_actions"] == ["backlog_close"]
    assert hint["issue_route_token_request"]["evidence_refs"] == ["timeline:22"]
    assert hint["issue_route_token_request"]["parent_route_token_ref"] == "rtok-parent"


def test_reference_only_contract_execution_id_does_not_trigger_terminal_close():
    route_identity = {
        "route_id": "route-root",
        "route_context_hash": "sha256:ctx-root",
        "prompt_contract_id": "rprompt-root",
        "prompt_contract_hash": "sha256:prompt-root",
        "visible_injection_manifest_hash": "sha256:visible-root",
        "route_token_ref": "rtok-parent",
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-root-terminal",
            "contract_execution_id": "cex-root-terminal",
            "contract_revision_id": "rev-root-terminal",
            "state": "selected",
            "route_identity": route_identity,
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(21, "route_context", payload={"route_context": route_identity}),
            _event(
                22,
                "close_ready",
                status="accepted",
                verification={
                    "contract_execution_id": "cex-root-terminal",
                    "scope": "reference_only",
                },
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "task-root-terminal",
            "target_files": json.dumps(["agent/governance/contract_state_runtime.py"]),
        },
    )

    action = projection["next_legal_action"]
    assert action["id"] == "close_ready"
    assert action["action"] == "record_close_ready"
    assert action["contract_execution_id"] == "cex-root-terminal"


def test_projection_exposes_active_contract_execution_handle():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-3",
            "state": "selected",
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    active = projection["active_contract_execution"]
    assert active["schema_version"] == "active_contract_execution.v1"
    assert active["project_id"] == "aming-claw"
    assert active["backlog_id"] == "AC-CONTRACT-RUNTIME"
    assert active["contract_id"] == "onboard_contract.v1"
    assert active["contract_template_id"] == "onboard_contract.v1"
    assert active["contract_revision_id"] == "rev-3"
    assert active["contract_execution_id"].startswith("cex-")
    assert active["contract_chain_id"].startswith("cchain-")
    assert projection["root_contract_execution"]["contract_execution_id"] == active[
        "contract_execution_id"
    ]
    assert projection["contract_chain"][0]["role"] == "root"
    executable = projection["executable_contract"]
    assert executable["active_contract_id"] == "onboard_contract.v1"
    assert executable["contract_execution"]["contract_execution_id"] == active[
        "contract_execution_id"
    ]
    assert executable["contract_execution"]["contract_chain_id"] == active[
        "contract_chain_id"
    ]
    assert executable["next_legal_operation"]["id"] == "route_context"
    assert executable["next_legal_operation"]["close_gate_is_navigation_source"] is False
    hints = projection["runtime_contract_hints"]
    assert hints["active_contract_execution_id"] == active["contract_execution_id"]
    assert hints["active_contract_chain_id"] == active["contract_chain_id"]
    assert hints["close_gate_policy"]["role"] == "final_verifier"
    assert hints["close_gate_policy"]["drives_next_legal_operation"] is False


def test_active_contract_binding_scopes_navigation_to_binding_watermark():
    onboard_template = {
        "template_id": "onboard_contract.v1",
        "required_evidence": [
            {
                "id": "route_context",
                "accepted_event_kinds": ["route_context"],
            },
            {
                "id": "route_action_precheck",
                "accepted_event_kinds": ["route_action_precheck"],
                "prerequisite_ids": ["route_context"],
            },
        ],
    }
    events = [
        _event(
            1,
            "mf_subagent_dispatch",
            payload={
                "task_id": "old-worker-a",
                "route_identity": {
                    "route_context_hash": "sha256:old",
                    "prompt_contract_id": "rprompt-old",
                    "route_token_ref": "rtok-old",
                },
            },
        ),
        _event(2, "implementation", payload={"task_id": "old-worker-b"}),
        _event(3, "verification", payload={"task_id": "old-worker-c"}),
        _event(
            10,
            "contract_binding",
            payload={
                "contract_binding": {
                    "contract_id": "onboard_contract.v1",
                    "contract_template_id": "onboard_contract.v1",
                    "contract_execution_id": "cex-fresh-onboard",
                    "contract_revision_id": "rev-fresh-onboard",
                    "state": "selected",
                },
                "route_identity": {
                    "route_id": "route-fresh",
                    "route_context_hash": "sha256:fresh",
                    "prompt_contract_id": "rprompt-fresh",
                    "prompt_contract_hash": "sha256:prompt-fresh",
                    "visible_injection_manifest_hash": "sha256:manifest-fresh",
                },
            },
        ),
    ]

    projection = build_contract_state_projection(
        events,
        contract={"schema_version": "legacy_bootstrap.v1"},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={"onboard_contract.v1": onboard_template},
    )

    assert projection["legacy_no_contract"] is False
    assert projection["contract_template_id"] == "onboard_contract.v1"
    assert projection["active_contract_execution"]["contract_execution_id"] == (
        "cex-fresh-onboard"
    )
    assert projection["completed_evidence"] == []
    assert projection["next_legal_action"]["id"] == "route_context"
    assert "cross_ref_lineage_bridge" not in {
        step["id"] for step in projection["ordered_next_steps"]
    }
    route_identity = projection["runtime_contract_hints"]["route_identity"]
    assert route_identity["route_context_hash"] == "sha256:fresh"
    assert route_identity["prompt_contract_id"] == "rprompt-fresh"
    assert "route_token_ref" not in route_identity


def test_runtime_hints_carry_role_bound_worker_prefill_boundary():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-prefill",
            "contract_execution_id": "cex-worker-prefill",
            "contract_chain_id": "cchain-worker-prefill",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "implementation",
                    "accepted_event_kinds": ["implementation"],
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "worker-task-1",
        },
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    prefill = hint["role_bound_prefill_policy"]
    assert action["contract_execution_id"] == "cex-worker-prefill"
    assert hint["actor_role"] == "mf_sub"
    assert prefill["execution_owner_role"] == "mf_sub"
    assert prefill["observer_owned"] is False
    assert prefill["actor_must_supply_evidence"] is True
    assert "route_identity" in prefill["observer_prefill_fields"]
    assert "changed_files" in prefill["actor_owned_execution_fields"]
    assert projection["runtime_contract_hints"]["next_legal_operation"][
        "execution_owner_role"
    ] == "mf_sub"


def test_mf_subagent_startup_next_action_hint_is_worker_owned():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-startup",
            "contract_execution_id": "cex-worker-startup",
            "contract_chain_id": "cchain-worker-startup",
            "state": "selected",
            "required_evidence": ["mf_subagent_startup"],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "worker-task-1",
        },
    )

    action = projection["next_legal_action"]
    hint = action["timeline_append_hint"]
    prefill = hint["role_bound_prefill_policy"]

    assert action["id"] == "mf_subagent_startup"
    assert hint["event_kind"] == "mf_subagent_startup"
    assert hint["satisfies_by"] == "event_kind"
    assert hint["actor_role"] == "mf_sub"
    assert hint["meta_contract_gate"]["allowed"] is True
    assert prefill["execution_owner_role"] == "mf_sub"
    assert prefill["observer_owned"] is False
    assert prefill["observer_prefill_allowed"] is True
    assert prefill["actor_must_supply_evidence"] is True
    assert "route_identity" in prefill["observer_prefill_fields"]
    assert "contract_execution_id" in prefill["observer_prefill_fields"]
    for field in (
        "status",
        "evidence_refs",
        "verification",
        "result",
        "changed_files",
        "tests",
        "graph_trace_ids",
    ):
        assert field not in prefill["observer_prefill_fields"]
        assert field in prefill["actor_owned_execution_fields"]


def test_mf_subagent_startup_missing_runtime_identity_does_not_complete_requirement():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-startup-invalid",
            "contract_execution_id": "cex-worker-startup-invalid",
            "contract_chain_id": "cchain-worker-startup-invalid",
            "state": "selected",
            "required_evidence": ["mf_subagent_startup"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                41,
                "mf_subagent_startup",
                status="ok",
                payload={
                    "contract_execution_id": "cex-worker-startup-invalid",
                    "actual_cwd": "/tmp/worker",
                    "actual_git_root": "/tmp/worker",
                    "known_missing_startup_fields": [
                        "runtime_context_id",
                        "fence_token",
                        "session_token_ref",
                    ],
                    "route_identity": {
                        "route_id": "route-startup",
                        "route_context_hash": "sha256:ctx",
                        "prompt_contract_id": "prompt-startup",
                        "prompt_contract_hash": "sha256:prompt",
                        "visible_injection_manifest_hash": "sha256:visible",
                    },
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["completed_evidence"] == []
    assert projection["missing_evidence"] == ["mf_subagent_startup"]
    assert projection["next_legal_action"]["id"] == "mf_subagent_startup"


def test_mf_subagent_startup_with_actual_identity_completes_requirement():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-worker-startup-valid",
            "contract_execution_id": "cex-worker-startup-valid",
            "contract_chain_id": "cchain-worker-startup-valid",
            "state": "selected",
            "required_evidence": ["mf_subagent_startup"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                42,
                "mf_subagent_startup",
                status="passed",
                payload={
                    "contract_execution_id": "cex-worker-startup-valid",
                    "actual_cwd": "/tmp/worker",
                    "actual_git_root": "/tmp/worker",
                    "branch": "refs/heads/codex/worker",
                    "head_commit": "abc123",
                    "fence_token": "fence-123",
                    "close_satisfying": True,
                    "runtime_context_id": "mfrctx-worker",
                    "task_id": "worker-task",
                    "parent_task_id": "AC-CONTRACT-RUNTIME",
                    "worker_slot_id": "worker-a",
                },
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["missing_evidence"] == []
    assert projection["completed_evidence"][0]["id"] == "mf_subagent_startup"
    assert projection["contract_complete"] is True


def test_projection_falls_back_to_event_project_id_for_active_execution():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-project-fallback",
            "state": "selected",
            "required_evidence": ["route_context"],
        }
    }

    projection = build_contract_state_projection(
        [_event(44, "route_context", project_id="aming-claw")],
        contract=contract,
        backlog_row={"bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["active_contract_execution"]["project_id"] == "aming-claw"


def test_non_onboard_contract_uses_same_projection_path():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-mf",
            "state": "selected",
            "required_evidence": [
                {"id": "implementation", "accepted_event_kinds": ["implementation"]},
                {"id": "verification", "accepted_event_kinds": ["verification"]},
            ],
        }
    }

    projection = build_contract_state_projection(
        [_event(50, "implementation", status="accepted")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_id"] == "mf_parallel.v1"
    assert [item["id"] for item in projection["completed_evidence"]] == [
        "implementation"
    ]
    assert projection["missing_evidence"] == ["verification"]
    assert projection["next_legal_action"]["id"] == "verification"


def test_builtin_mf_parallel_requirements_drive_role_bound_next_action_order():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_revision_id": "rev-mf-defaults",
            "state": "bound",
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=[
            "close_ready",
            "implementation",
            "verification",
        ],
    )

    assert projection["requirements_explicit"] is True
    assert projection["missing_evidence"] == [
        "observer_prefill_child_contracts",
        "observer_dispatch_bounded_workers",
        "worker_read_runtime_guide",
        "worker_startup",
        "worker_graph_context",
        "worker_implementation",
        "worker_finish_time_attestation",
        "worker_finish_gate",
        "qa_independent_verification",
        "observer_merge_queue_item_materialize",
        "observer_merge",
        "observer_reconcile",
        "observer_close_ready",
    ]
    assert projection["next_legal_action"]["id"] == "observer_prefill_child_contracts"
    assert projection["next_legal_action"]["timeline_append_hint"]["event_kind"] == (
        "contract_binding"
    )
    assert projection["next_legal_action"]["timeline_append_hint"]["actor_role"] == (
        "observer"
    )
    assert projection["next_legal_action"]["contract_execution_id"] == projection[
        "active_contract_execution"
    ]["contract_execution_id"]
    assert projection["active_lane_contract"]["next_legal_action"]["id"] == (
        "observer_prefill_child_contracts"
    )

    worker_read = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    action = worker_read["next_legal_action"]
    assert action["id"] == "worker_read_runtime_guide"
    assert action["timeline_append_hint"]["event_kind"] == "mf_subagent_read_receipt"
    assert action["timeline_append_hint"]["actor_role"] == "mf_sub"
    assert action["timeline_append_hint"]["meta_contract_gate"]["allowed"] is True

    qa = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
            _event(3, "mf_subagent_read_receipt"),
            _event(4, "mf_subagent_startup"),
            _event(5, "graph_trace"),
            _event(6, "implementation"),
            _event(7, "record_finish_time_worker_attestation"),
            _event(8, "mf_subagent_finish_gate"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert qa["next_legal_action"]["id"] == "qa_independent_verification"
    assert qa["next_legal_action"]["timeline_append_hint"]["event_kind"] == (
        "independent_verification"
    )
    assert qa["next_legal_action"]["timeline_append_hint"]["actor_role"] == "qa"

    materialize = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
            _event(3, "mf_subagent_read_receipt"),
            _event(4, "mf_subagent_startup"),
            _event(5, "graph_trace"),
            _event(6, "implementation"),
            _event(7, "record_finish_time_worker_attestation"),
            _event(8, "mf_subagent_finish_gate"),
            _event(9, "independent_verification"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert materialize["next_legal_action"]["id"] == (
        "observer_merge_queue_item_materialize"
    )
    assert materialize["next_legal_action"]["timeline_append_hint"]["event_kind"] == (
        "merge_queue_item_materialize"
    )
    assert materialize["next_legal_action"]["timeline_append_hint"]["actor_role"] == (
        "observer"
    )

    merge = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
            _event(3, "mf_subagent_read_receipt"),
            _event(4, "mf_subagent_startup"),
            _event(5, "graph_trace"),
            _event(6, "implementation"),
            _event(7, "record_finish_time_worker_attestation"),
            _event(8, "mf_subagent_finish_gate"),
            _event(9, "independent_verification"),
            _event(10, "merge_queue_item_materialize"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert merge["next_legal_action"]["id"] == "observer_merge"
    assert merge["next_legal_action"]["timeline_append_hint"]["event_kind"] == "merge"
    assert merge["next_legal_action"]["timeline_append_hint"]["actor_role"] == (
        "observer"
    )


def test_builtin_mf_parallel_v2_requirements_transition_finish_directly_to_qa_graph():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v2",
            "contract_template_id": "mf_parallel.v2",
            "contract_revision_id": "rev-mf-v2-defaults",
            "state": "bound",
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["missing_evidence"] == [
        "observer_prefill_child_contracts",
        "observer_dispatch_bounded_workers",
        "worker_read_runtime_guide",
        "worker_startup",
        "worker_graph_context",
        "worker_implementation",
        "worker_commit",
        "worker_finish_time_attestation",
        "worker_finish_gate",
        "qa_graph_context",
        "qa_independent_verification",
        "observer_merge_queue_item_materialize",
        "observer_merge",
        "observer_reconcile",
        "observer_close_ready",
    ]
    assert "worker_review_ready_handoff" not in projection["required_evidence"]

    qa_graph = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
            _event(3, "mf_subagent_read_receipt"),
            _event(4, "mf_subagent_startup"),
            _event(5, "graph_trace"),
            _event(6, "implementation"),
            _event(7, "worker_commit"),
            _event(8, "record_finish_time_worker_attestation"),
            _event(9, "mf_subagent_finish_gate"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert qa_graph["next_legal_action"]["id"] == "qa_graph_context"
    assert qa_graph["next_legal_action"]["timeline_append_hint"]["event_kind"] == "qa_graph_trace"
    assert qa_graph["next_legal_action"]["timeline_append_hint"]["actor_role"] == "qa"

    qa = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
            _event(3, "mf_subagent_read_receipt"),
            _event(4, "mf_subagent_startup"),
            _event(5, "graph_trace"),
            _event(6, "implementation"),
            _event(7, "worker_commit"),
            _event(8, "record_finish_time_worker_attestation"),
            _event(9, "mf_subagent_finish_gate"),
            _event(10, "qa_graph_trace"),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert qa["next_legal_action"]["id"] == "qa_independent_verification"

    bounded_qa = build_contract_state_projection(
        [
            _event(1, "contract_binding"),
            _event(2, "dispatch_bounded_worker"),
            _event(3, "mf_subagent_read_receipt"),
            _event(4, "mf_subagent_startup"),
            _event(5, "graph_trace"),
            _event(6, "implementation"),
            _event(7, "worker_commit"),
            _event(8, "record_finish_time_worker_attestation"),
            _event(9, "mf_subagent_finish_gate"),
            _event(
                10,
                "qa_graph_trace",
                payload={"graph_trace_evidence": _bounded_qa_graph_evidence()},
            ),
        ],
        contract=_strict_mf_parallel_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert bounded_qa["next_legal_action"]["id"] == "qa_independent_verification"
    assert bounded_qa["next_legal_action"]["timeline_append_hint"]["actor_role"] == "qa"


def test_bounded_qa_graph_context_requires_complete_candidate_review_tuple():
    contract = _strict_mf_parallel_contract()
    prefix = [
        _event(1, "contract_binding"),
        _event(2, "dispatch_bounded_worker"),
        _event(3, "mf_subagent_read_receipt"),
        _event(4, "mf_subagent_startup"),
        _event(5, "graph_trace"),
        _event(6, "implementation"),
        _event(7, "worker_commit"),
        _event(8, "record_finish_time_worker_attestation"),
        _event(9, "mf_subagent_finish_gate"),
        _event(10, "review_ready"),
    ]
    incomplete = build_contract_state_projection(
        [
            *prefix,
            _event(
                11,
                "qa_graph_trace",
                payload={
                    "graph_trace_evidence": {
                        "qa_session_id": "ses-qa-bounded",
                        "graph_basis": "canonical_base_plus_candidate_diff",
                        "canonical_base_snapshot_id": "full-base",
                        "base_commit_sha": "a" * 40,
                        "changed_files": ["agent/governance/server.py"],
                        "candidate_diff_hash": "sha256:" + "c" * 64,
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert incomplete["next_legal_action"]["id"] == "qa_graph_context"

    complete = build_contract_state_projection(
        [
            *prefix,
            _event(
                11,
                "qa_graph_trace",
                payload={
                    "graph_trace_evidence": _bounded_qa_graph_evidence(
                        base_diff=True
                    )
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert complete["next_legal_action"]["id"] == "qa_independent_verification"


def test_qa_and_reconcile_authority_fields_cannot_be_combined_across_mappings():
    qa_evidence = _bounded_qa_graph_evidence(base_diff=True)
    assert _bounded_qa_graph_context_satisfies_requirement(
        {"payload": {"graph_trace_evidence": qa_evidence}}
    ) is True
    split_qa = dict(qa_evidence)
    candidate_diff_hash = split_qa.pop("candidate_diff_hash")
    assert _bounded_qa_graph_context_satisfies_requirement(
        {
            "payload": {
                "graph_trace_evidence": split_qa,
                "candidate_review_context": {
                    "candidate_diff_hash": candidate_diff_hash,
                },
            }
        }
    ) is False

    commit_sha = "c" * 40
    marker = {
        "schema_version": "current_full_reconcile.provenance.v2",
        "protected_action": "graph_current_full_reconcile",
        "protected_entrypoint": (
            "POST /api/graph-governance/{project_id}/reconcile/current-full"
        ),
        "provenance_id": "cfrp-contract-state",
        "provenance_hash": "sha256:" + "9" * 64,
        "request_id": "req-contract-state",
        "request_started_at": "2026-07-11T01:00:30Z",
        "marker_created_at": "2026-07-11T01:01:01Z",
        "target_commit_sha": commit_sha,
        "snapshot_id": "full-coherent-reconcile",
        "reconcile_event_id": 12,
        "reconcile_event_created_at": "2026-07-11T01:01:00Z",
        "route_evidence": {
            "schema_version": "graph_current_full_reconcile.route_evidence.v1",
            "raw_route_token_persisted": False,
            "protected_action": "graph_current_full_reconcile",
        },
    }
    reconcile_authority = {
        "source": "graph_snapshot_store.current_full_reconcile_state",
        "db_verified": True,
        "live_verified": True,
        "current_full_reconcile": True,
        "strategy": "current_full_reconcile",
        "active_snapshot_id": "full-coherent-reconcile",
        "merged_commit_sha": commit_sha,
        "reconciled_commit_sha": commit_sha,
        "canonical_head_commit": commit_sha,
        "active_snapshot_commit": commit_sha,
        "graph_reconciled": True,
        "canonical_head_verified": True,
        "active_snapshot_verified": True,
        "provenance_verified": True,
        "provenance_scope_verified": True,
        "durable_order_verified": True,
        "qa_event_id": 10,
        "qa_event_created_at": "2026-07-11T00:59:00Z",
        "merge_event_id": 11,
        "merge_event_created_at": "2026-07-11T01:00:00Z",
        "reconcile_event_id": 12,
        "reconcile_event_created_at": "2026-07-11T01:01:00Z",
        "current_full_reconcile_marker": marker,
        "current_full_reconcile_provenance": {
            "provenance_id": marker["provenance_id"],
            "provenance_hash": marker["provenance_hash"],
            "protected_action": marker["protected_action"],
            "protected_entrypoint": marker["protected_entrypoint"],
            "request_id": marker["request_id"],
            "request_started_at": marker["request_started_at"],
            "marker_created_at": marker["marker_created_at"],
            "reconcile_event_id": marker["reconcile_event_id"],
            "reconcile_event_created_at": marker[
                "reconcile_event_created_at"
            ],
        },
    }
    assert _current_full_reconcile_satisfies_requirement(
        {"payload": {"reconcile_authority": reconcile_authority}}
    ) is True
    split_reconcile = dict(reconcile_authority)
    active_snapshot_commit = split_reconcile.pop("active_snapshot_commit")
    assert _current_full_reconcile_satisfies_requirement(
        {
            "payload": {
                "reconcile_authority": split_reconcile,
                "live_graph_state": {
                    "active_snapshot_commit": active_snapshot_commit,
                },
            }
        }
    ) is False


def test_rev2_projection_requires_ids_and_timestamps_for_qa_merge_reconcile():
    commit_sha = "d" * 40
    marker = {
        "schema_version": "current_full_reconcile.provenance.v2",
        "protected_action": "graph_current_full_reconcile",
        "protected_entrypoint": (
            "POST /api/graph-governance/{project_id}/reconcile/current-full"
        ),
        "provenance_id": "cfrp-contract-state-order",
        "provenance_hash": "sha256:" + "8" * 64,
        "request_id": "req-contract-state-order",
        "request_started_at": "2026-07-11T01:02:30Z",
        "marker_created_at": "2026-07-11T01:03:01Z",
        "target_commit_sha": commit_sha,
        "snapshot_id": "full-contract-state-order",
        "reconcile_event_id": 3,
        "reconcile_event_created_at": "2026-07-11T01:03:00Z",
        "route_evidence": {
            "schema_version": "graph_current_full_reconcile.route_evidence.v1",
            "raw_route_token_persisted": False,
            "protected_action": "graph_current_full_reconcile",
        },
    }
    authority = {
        "source": "graph_snapshot_store.current_full_reconcile_state",
        "db_verified": True,
        "live_verified": True,
        "current_full_reconcile": True,
        "strategy": "current_full_reconcile",
        "active_snapshot_id": marker["snapshot_id"],
        "merged_commit_sha": commit_sha,
        "reconciled_commit_sha": commit_sha,
        "canonical_head_commit": commit_sha,
        "active_snapshot_commit": commit_sha,
        "graph_reconciled": True,
        "canonical_head_verified": True,
        "active_snapshot_verified": True,
        "provenance_verified": True,
        "provenance_scope_verified": True,
        "durable_order_verified": True,
        "qa_event_id": 1,
        "qa_event_created_at": "2026-07-11T01:01:00Z",
        "merge_event_id": 2,
        "merge_event_created_at": "2026-07-11T01:02:00Z",
        "reconcile_event_id": 3,
        "reconcile_event_created_at": "2026-07-11T01:03:00Z",
        "current_full_reconcile_marker": marker,
        "current_full_reconcile_provenance": {
            "provenance_id": marker["provenance_id"],
            "provenance_hash": marker["provenance_hash"],
            "protected_action": marker["protected_action"],
            "protected_entrypoint": marker["protected_entrypoint"],
            "request_id": marker["request_id"],
            "request_started_at": marker["request_started_at"],
            "marker_created_at": marker["marker_created_at"],
            "reconcile_event_id": marker["reconcile_event_id"],
            "reconcile_event_created_at": marker["reconcile_event_created_at"],
        },
    }
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v2",
            "contract_template_id": "mf_parallel.v2",
            "contract_revision_id": "rev2",
            "state": "bound",
            "evidence_requirements": [
                {
                    "id": "qa_independent_verification",
                    "event_kind": "independent_verification",
                },
                {
                    "id": "observer_merge",
                    "event_kind": "merge",
                    "requires": ["qa_independent_verification"],
                },
                {
                    "id": "observer_reconcile",
                    "event_kind": "reconcile",
                    "requires": ["observer_merge"],
                },
            ],
            "server_derived_evidence_policies": {
                "current_full_reconcile_evidence_policy": {
                    "enabled": True,
                    "line_ids": ["observer_reconcile"],
                }
            },
        }
    }

    def projection(qa_created_at: str):
        return build_contract_state_projection(
            [
                _event(
                    1,
                    "independent_verification",
                    created_at=qa_created_at,
                ),
                _event(
                    2,
                    "merge",
                    created_at="2026-07-11T01:02:00Z",
                    commit_sha=commit_sha,
                ),
                _event(
                    3,
                    "reconcile",
                    created_at="2026-07-11T01:03:00Z",
                    payload={"reconcile_authority": authority},
                ),
            ],
            contract=contract,
            backlog_row={
                "project_id": "aming-claw",
                "bug_id": "AC-CONTRACT-RUNTIME",
            },
        )

    contradictory = projection("2026-07-11T01:04:00Z")
    assert contradictory["contract_complete"] is False
    assert "observer_merge" in contradictory["missing_evidence"]

    valid = projection("2026-07-11T01:01:00Z")
    assert valid["contract_complete"] is True
    assert valid["missing_evidence"] == []


def test_builtin_mf_parallel_v2_req_2ff0e242e70f_requires_commit_after_implementation():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v2",
            "contract_template_id": "mf_parallel.v2",
            "contract_revision_id": "rev-mf-v2-worker-commit",
            "state": "bound",
        }
    }
    through_implementation = [
        _event(1, "contract_binding"),
        _event(2, "dispatch_bounded_worker"),
        _event(3, "mf_subagent_read_receipt"),
        _event(4, "mf_subagent_startup"),
        _event(5, "graph_trace"),
        _event(6, "implementation"),
    ]

    projection = build_contract_state_projection(
        through_implementation,
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert projection["next_legal_action"]["id"] == "worker_commit"
    assert "worker_finish_time_attestation" in projection["missing_evidence"]

    premature_attestation = build_contract_state_projection(
        [*through_implementation, _event(7, "record_finish_time_worker_attestation")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    assert premature_attestation["next_legal_action"]["id"] == "worker_commit"
    assert "worker_finish_time_attestation" in premature_attestation["missing_evidence"]


def test_finish_gate_completion_satisfies_worker_startup_and_finish_requirements():
    contract = {
        "contract": {
            "contract_id": "mf_parallel.v1",
            "contract_template_id": "mf_parallel.v1",
            "contract_execution_id": "cex-parallel-root",
            "state": "selected",
            "required_evidence": [
                "mf_subagent_startup",
                "mf_subagent_finish_gate",
                "independent_verification_lane",
            ],
        }
    }
    finish_gate = _event(
        23,
        "mf_subagent_finish_gate",
        payload={
            "mf_subagent_finish_gate": {
                "close_ready": True,
                "receipt_gate": {
                    "status": "passed",
                    "read_receipt_present": True,
                    "startup_present": True,
                },
                "startup_worker_identity_gate": {"passed": True},
                "worker_self_attestation_gate": {"passed": True},
                "parent_task_id": "AC-CONTRACT-RUNTIME",
                "task_id": "AC-CONTRACT-RUNTIME-worker-a",
                "runtime_context_id": "mfrctx-worker-a",
            }
        },
    )

    projection = build_contract_state_projection(
        [finish_gate],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    completed = {item["id"] for item in projection["completed_evidence"]}
    assert "mf_subagent_startup" in completed
    assert "mf_subagent_finish_gate" in completed
    assert projection["missing_evidence"] == ["independent_verification_lane"]
    assert projection["next_legal_action"]["id"] == "independent_verification_lane"


def test_bound_root_default_requirements_drive_next_action_without_active_execution():
    projection = build_contract_state_projection(
        [
            _event(
                80,
                "contract_state_changed",
                payload={"contract_binding": {"state": "bound"}},
            )
        ],
        contract={},
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        default_required_evidence=[
            "mf_subagent_startup",
            "implementation",
            "verification",
            "close_ready",
        ],
    )

    assert projection["state"] == "bound"
    assert projection["legacy_no_contract"] is False
    assert projection["active_contract_execution"] == {}
    assert projection["missing_evidence"] == [
        "mf_subagent_startup",
        "implementation",
        "verification",
        "close_ready",
    ]
    action = projection["next_legal_action"]
    assert action["id"] == "mf_subagent_startup"
    assert action["backlog_id"] == "AC-CONTRACT-RUNTIME"
    assert action["contract_execution_id"] == ""
    assert projection["runtime_contract_hints"]["next_legal_operation"]["id"] == (
        "mf_subagent_startup"
    )
    assert projection["executable_contract"]["next_legal_operation"]["id"] == (
        "mf_subagent_startup"
    )


def test_onboard_complete_exposes_successor_candidates_as_next_action():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-successor",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                    {"contract_template_id": "mf_parallel.v1"},
                ]
            },
        }
    }

    projection = build_contract_state_projection(
        [_event(60, "route_context")],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is True
    assert [item["contract_template_id"] for item in projection["successor_contract_candidates"]] == [
        "observer_hotfix_direct_mutation.v1",
        "mf_parallel.v1",
    ]
    assert projection["successor_next_legal_action"]["id"] == "select_successor_contract"
    assert projection["next_legal_action"]["id"] == "select_successor_contract"
    assert projection["next_legal_action"]["contract_chain_id"] == projection[
        "contract_chain_id"
    ]
    assert projection["next_legal_action"]["successor_contract_policy"][
        "selection_action"
    ] == "select_successor_contract"


def test_successor_binding_selects_latest_successor_with_distinct_execution_ids():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-successor",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-successor-binding",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [{"contract_template_id": "review_contract.v1"}]
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(70, "route_context"),
            _event(
                71,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-successor",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "first review",
                    }
                },
            ),
            _event(
                72,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-successor",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "second review",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    successors = [
        item for item in projection["contract_chain"] if item["role"] == "successor"
    ]
    assert len(successors) == 2
    assert successors[0]["successor_contract_execution_id"] != successors[1][
        "successor_contract_execution_id"
    ]
    assert projection["selected_successor_contract"]["handoff_reason"] == "second review"
    assert projection["successor_next_legal_action"]["contract_execution_id"] == (
        projection["selected_successor_contract"]["successor_contract_execution_id"]
    )


def test_successor_binding_requires_chain_and_parent_match():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-successor",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-successor-binding",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [{"contract_template_id": "review_contract.v1"}]
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(75, "route_context"),
            _event(
                76,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "missing parent and chain",
                    }
                },
            ),
            _event(
                77,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-other",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "wrong chain",
                    }
                },
            ),
            _event(
                78,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-successor",
                        "parent_contract_execution_id": "cex-other-root",
                        "contract_template_id": "review_contract.v1",
                        "handoff_reason": "wrong parent",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["selected_successor_contract"] == {}
    assert projection["successor_next_legal_action"]["id"] == "select_successor_contract"


def test_requirement_evidence_is_scoped_by_contract_execution_id():
    contract = {
        "contract": {
            "contract_id": "review_contract.v1",
            "contract_template_id": "review_contract.v1",
            "contract_execution_id": "cex-review-expected",
            "contract_revision_id": "rev-review",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "review_done",
                    "accepted_event_kinds": ["review_lane"],
                    "contract_execution_id": "cex-review-expected",
                }
            ],
        }
    }

    wrong = build_contract_state_projection(
        [
            _event(
                80,
                "review_lane",
                payload={"contract_execution_id": "cex-review-other"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    reference_only = build_contract_state_projection(
        [
            _event(
                82,
                "review_lane",
                payload={"requirement_id": "review_done"},
                verification={"contract_execution_id": "cex-review-expected"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )
    right = build_contract_state_projection(
        [
            _event(
                81,
                "review_lane",
                payload={"contract_execution_id": "cex-review-expected"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert wrong["completed_evidence"] == []
    assert wrong["missing_evidence"] == ["review_done"]
    assert reference_only["completed_evidence"] == []
    assert reference_only["missing_evidence"] == ["review_done"]
    assert [item["id"] for item in right["completed_evidence"]] == ["review_done"]
    assert right["missing_evidence"] == []


def test_selected_hotfix_successor_uses_template_requirements_before_next_successor():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-onboard-hotfix",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {
                "id": "hotfix_pre_reason",
                "event_kind": "hotfix_entered",
            },
            {
                "id": "hotfix_post_action_summary",
                "event_kind": "hotfix_under_action",
            },
        ],
        "successor_contract_policy": {
            "selection_action": "select_successor_contract",
            "candidates": [
                {"contract_template_id": "mf_parallel.v1"},
                {"contract_template_id": "qa_evidence_gate_review.v1"},
            ],
        },
    }

    projection = build_contract_state_projection(
        [
            _event(90, "route_context"),
            _event(
                91,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                        "handoff_reason": "workflow hotfix",
                    }
                },
            ),
            _event(
                92,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    state = projection["selected_successor_contract_state"]
    assert state["contract_execution_id"] == "cex-hotfix"
    assert state["completed_evidence"][0]["id"] == "hotfix_pre_reason"
    assert state["missing_evidence"] == ["hotfix_post_action_summary"]
    assert projection["next_legal_action"]["id"] == "hotfix_post_action_summary"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-hotfix"

    completed = build_contract_state_projection(
        [
            _event(90, "route_context"),
            _event(
                91,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                        "handoff_reason": "workflow hotfix",
                    }
                },
            ),
            _event(
                92,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                93,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    completed_state = completed["selected_successor_contract_state"]
    assert completed_state["contract_complete"] is True
    assert completed["next_legal_action"]["id"] == "select_successor_contract"
    assert completed["next_legal_action"]["contract_execution_id"] == "cex-hotfix"
    assert [
        item["contract_template_id"]
        for item in completed["next_legal_action"]["successor_contract_candidates"]
    ] == ["mf_parallel.v1", "qa_evidence_gate_review.v1"]


def test_successor_missing_step_does_not_inherit_selection_event_id():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-selection-pollution",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-selection-pollution",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(5920, "route_context"),
            _event(
                5921,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-selection-pollution",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    state = projection["selected_successor_contract_state"]
    assert state["missing_evidence"] == [
        "hotfix_pre_reason",
        "hotfix_post_action_summary",
    ]
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"
    for step in state["ordered_next_steps"]:
        assert "5921" not in step.get("accepted_event_ids", [])
    for step in projection["next_legal_action"]["ordered_missing_steps"]:
        assert "5921" not in step.get("accepted_event_ids", [])


def test_nested_successor_binding_after_hotfix_is_parent_scoped():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-hotfix-nested",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-onboard-hotfix",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }

    projection = build_contract_state_projection(
        [
            _event(100, "route_context"),
            _event(
                101,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-nested",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                102,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                103,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                104,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-nested",
                        "parent_contract_execution_id": "cex-other-parent",
                        "successor_contract_execution_id": "cex-wrong-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                105,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-nested",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    nested = projection["selected_successor_contract_state"][
        "selected_successor_contract_binding"
    ]
    assert nested["contract_execution_id"] == "cex-qa"
    assert projection["next_legal_action"]["id"] == "successor_contract_selected"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa"


def test_successor_requirements_accept_handoff_and_parent_execution_evidence():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-hotfix-lineage",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-onboard-hotfix",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
            {
                "id": "focused_verification",
                "event_kind": "verification",
                "match_policy": "canonical_event_kind",
            },
            {"id": "close_ready", "event_kind": "close_ready"},
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(150, "route_context"),
            _event(151, "hotfix_entered"),
            _event(
                152,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-hotfix-lineage",
                        "parent_contract_execution_id": "cex-onboard-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                        "handoff_event_id": "151",
                    }
                },
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "verification",
                payload={
                    "contract_execution_id": "cex-qa",
                    "parent_contract_execution_id": "cex-hotfix",
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    state = projection["selected_successor_contract_state"]
    assert [item["id"] for item in state["completed_evidence"]] == [
        "hotfix_pre_reason",
        "hotfix_post_action_summary",
        "focused_verification",
    ]
    assert state["missing_evidence"] == ["close_ready"]
    assert projection["next_legal_action"]["id"] == "close_ready"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-hotfix"


def test_custom_requirement_next_action_recommends_contract_state_changed_wrapper():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_revision_id": "rev-custom-evidence",
            "state": "selected",
            "required_evidence": [
                {
                    "id": "related_backlog_review",
                    "action": "record_related_backlog_review",
                }
            ],
        }
    }

    projection = build_contract_state_projection(
        [],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    next_action = projection["next_legal_action"]
    hint = next_action["timeline_append_hint"]
    assert next_action["id"] == "related_backlog_review"
    assert hint["event_kind"] == "contract_state_changed"
    assert hint["satisfies_by"] == "payload.requirement_id"
    assert hint["payload"]["requirement_id"] == "related_backlog_review"

    completed = build_contract_state_projection(
        [
            _event(
                110,
                "contract_state_changed",
                payload={"requirement_id": "related_backlog_review"},
            )
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert completed["contract_complete"] is True
    assert completed["missing_evidence"] == []


def test_top_level_successor_binding_does_not_rebind_root_contract():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-top-level-successor",
            "contract_execution_id": "cex-onboard-root",
            "contract_revision_id": "rev-top-level-successor",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"}
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(120, "route_context"),
            _event(
                121,
                "contract_binding",
                payload={
                    "contract_chain_id": "cchain-top-level-successor",
                    "parent_contract_execution_id": "cex-onboard-root",
                    "successor_contract_execution_id": "cex-hotfix",
                    "contract_id": "observer_hotfix_direct_mutation.v1",
                    "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    "handoff_reason": "workflow hotfix",
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    assert projection["contract_id"] == "onboard_contract.v1"
    assert projection["active_contract_execution"]["contract_execution_id"] == (
        "cex-onboard-root"
    )
    assert projection["selected_successor_contract"]["contract_id"] == (
        "observer_hotfix_direct_mutation.v1"
    )
    assert projection["selected_successor_contract_state"]["contract_execution_id"] == (
        "cex-hotfix"
    )
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"


def test_binding_execution_id_drives_successor_parent_scope_and_lane_runtime():
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-bound-root-successor",
            "contract_revision_id": "rev-bound-root-successor",
            "state": "selected",
            "required_evidence": ["route_context"],
            "successor_contract_policy": {
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ]
            },
        }
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"}
        ],
    }

    projection = build_contract_state_projection(
        [
            _event(130, "route_context"),
            _event(
                131,
                "contract_binding",
                payload={
                    "contract_binding": {
                        "contract_chain_id": "cchain-bound-root-successor",
                        "contract_execution_id": "cex-onboard-bound",
                        "contract_id": "onboard_contract.v1",
                        "contract_template_id": "onboard_contract.v1",
                        "contract_revision_id": "rev-bound-root-successor",
                        "state": "selected",
                    },
                    "successor_contract": {
                        "contract_chain_id": "cchain-bound-root-successor",
                        "parent_contract_execution_id": "cex-onboard-bound",
                        "successor_contract_execution_id": "cex-hotfix-bound",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    },
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
        },
    )

    assert projection["active_contract_execution"]["contract_execution_id"] == (
        "cex-onboard-bound"
    )
    assert projection["selected_successor_contract"]["contract_execution_id"] == (
        "cex-hotfix-bound"
    )
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-hotfix-bound"
    )
    assert projection["active_lane_contract"]["contract_execution_id"] == (
        "cex-hotfix-bound"
    )
    assert projection["contract_execution_index"]["cex-onboard-bound"]["role"] == "root"
    assert (
        projection["contract_execution_index"]["cex-hotfix-bound"][
            "parent_contract_execution_id"
        ]
        == "cex-onboard-bound"
    )


def test_qa_hotfix_qa_successor_path_exposes_nested_lane_next_action():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa",
            "contract_execution_id": "cex-qa-root",
            "contract_revision_id": "rev-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                140,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-root"},
            ),
            _event(
                141,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                142,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                143,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                144,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert [
        item["contract_execution_id"]
        for item in projection["contract_lane_executions"]
    ] == ["cex-qa-root", "cex-hotfix", "cex-qa-followup"]
    assert projection["active_lane_contract"]["role"] == "nested_successor"
    assert projection["active_lane_contract"]["contract_template_id"] == (
        "qa_evidence_gate_review.v1"
    )
    assert projection["next_legal_action"]["id"] == "qa_review"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-qa-followup"
    )
    assert projection["next_legal_action"]["ordered_missing_steps_source"] == (
        "nested_successor_contract_state"
    )
    assert projection["contract_execution_index"]["cex-hotfix"]["missing_evidence"] == []
    assert projection["contract_execution_index"]["cex-qa-followup"][
        "missing_evidence"
    ] == ["qa_review"]


def test_completed_hotfix_qa_successor_prompts_close_ready_not_hotfix_loop():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                {"contract_template_id": "audit_close_with_qa_acceptance.v1"},
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix-qa-complete",
            "contract_execution_id": "cex-onboard",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ],
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                170,
                "contract_state_changed",
                payload={"requirement_id": "graph_query_schema_trace"},
            ),
            _event(
                171,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-complete",
                        "parent_contract_execution_id": "cex-onboard",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                172,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                173,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                174,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-complete",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(175, "qa_review", payload={"contract_execution_id": "cex-qa"}),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert projection["active_lane_contract"]["contract_execution_id"] == "cex-qa"
    assert projection["active_lane_contract"]["missing_evidence"] == []
    assert projection["next_legal_action"]["id"] == "close_ready"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa"
    assert projection["next_legal_action"]["timeline_append_hint"][
        "terminal_semantics"
    ]["audit_close_candidate"] is True
    assert projection["next_legal_action"]["timeline_append_hint"][
        "terminal_semantics"
    ]["hotfix_candidate"] is True


def test_completed_hotfix_qa_successor_with_close_ready_prompts_backlog_close():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                {"contract_template_id": "audit_close_with_qa_acceptance.v1"},
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix-qa-close",
            "contract_execution_id": "cex-onboard",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ],
            },
        }
    }

    projection = build_contract_state_projection(
        [
            _event(
                180,
                "contract_state_changed",
                payload={"requirement_id": "graph_query_schema_trace"},
            ),
            _event(
                181,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-close",
                        "parent_contract_execution_id": "cex-onboard",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                182,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                183,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                184,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-onboard-hotfix-qa-close",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(185, "qa_review", payload={"contract_execution_id": "cex-qa"}),
            _event(
                186,
                "close_ready",
                status="accepted",
                payload={"contract_execution_id": "cex-qa"},
            ),
        ],
        contract=contract,
        backlog_row={
            "project_id": "aming-claw",
            "bug_id": "AC-CONTRACT-RUNTIME",
            "task_id": "task-hotfix-qa-close",
            "target_files": json.dumps(["agent/governance/contract_state_runtime.py"]),
        },
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    action = projection["next_legal_action"]
    assert projection["active_lane_contract"]["contract_execution_id"] == "cex-qa"
    assert action["id"] == "issue_backlog_close_route_token_then_backlog_close"
    assert action["action"] == "backlog_close"
    assert action["contract_execution_id"] == "cex-qa"
    assert action["route_token_request_hint"]["allowed_actions"] == ["backlog_close"]
    assert action["route_token_request_hint"]["evidence_refs"] == ["timeline:186"]
    lane_action = projection["active_lane_contract"]["next_legal_action"]
    assert lane_action["protected_action"] is True
    assert lane_action["allowed_action"] == "backlog_close"
    assert lane_action["route_token_request_hint"]["allowed_actions"] == [
        "backlog_close"
    ]
    assert lane_action["route_token_request_hint"]["evidence_refs"] == ["timeline:186"]
    assert lane_action["close_ready_evidence_refs"][0]["ref"] == "timeline:186"


def test_completed_hotfix_qa_followup_prompts_successor_selection():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"},
                {"contract_template_id": "audit_close_with_qa_acceptance.v1"},
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "onboard_contract.v1",
            "contract_template_id": "onboard_contract.v1",
            "contract_chain_id": "cchain-onboard-hotfix-qa-followup",
            "contract_execution_id": "cex-onboard",
            "state": "selected",
            "required_evidence": ["graph_query_schema_trace"],
            "successor_contract_policy": {
                "selection_action": "select_successor_contract",
                "candidates": [
                    {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
                ],
            },
        }
    }

    for event_id, gate_decision in ((175, "pass_with_followups"), (176, "block")):
        projection = build_contract_state_projection(
            [
                _event(
                    170,
                    "contract_state_changed",
                    payload={"requirement_id": "graph_query_schema_trace"},
                ),
                _event(
                    171,
                    "contract_binding",
                    payload={
                        "successor_contract": {
                            "contract_chain_id": (
                                "cchain-onboard-hotfix-qa-followup"
                            ),
                            "parent_contract_execution_id": "cex-onboard",
                            "successor_contract_execution_id": "cex-hotfix",
                            "contract_template_id": (
                                "observer_hotfix_direct_mutation.v1"
                            ),
                        }
                    },
                ),
                _event(
                    172,
                    "hotfix_entered",
                    payload={"successor_contract_execution_id": "cex-hotfix"},
                ),
                _event(
                    173,
                    "hotfix_under_action",
                    payload={"successor_contract_execution_id": "cex-hotfix"},
                ),
                _event(
                    174,
                    "contract_binding",
                    payload={
                        "successor_contract": {
                            "contract_chain_id": (
                                "cchain-onboard-hotfix-qa-followup"
                            ),
                            "parent_contract_execution_id": "cex-hotfix",
                            "successor_contract_execution_id": "cex-qa",
                            "contract_template_id": "qa_evidence_gate_review.v1",
                        }
                    },
                ),
                _event(
                    event_id,
                    "qa_review",
                    payload={
                        "contract_execution_id": "cex-qa",
                        "gate_decision": gate_decision,
                    },
                ),
            ],
            contract=contract,
            backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
            contract_templates={
                "observer_hotfix_direct_mutation.v1": hotfix_template,
                "qa_evidence_gate_review.v1": qa_template,
            },
        )

        assert projection["active_lane_contract"]["contract_execution_id"] == "cex-qa"
        assert projection["active_lane_contract"]["missing_evidence"] == []
        assert projection["next_legal_action"]["id"] == "select_successor_contract"
        assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa"
        assert "observer_hotfix_direct_mutation.v1" in [
            item["contract_template_id"]
            for item in projection["next_legal_action"][
                "successor_contract_candidates"
            ]
        ]


def test_qa_followup_hotfix_successor_exposes_deep_lane_next_action():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert [
        item["contract_execution_id"]
        for item in projection["contract_lane_executions"]
    ] == ["cex-qa-root", "cex-hotfix", "cex-qa-followup", "cex-hotfix-r2"]
    assert projection["active_lane_contract"]["role"] == "deep_successor"
    assert projection["active_lane_contract"]["contract_template_id"] == (
        "observer_hotfix_direct_mutation.v1"
    )
    assert projection["next_legal_action"]["id"] == "hotfix_pre_reason"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-hotfix-r2"
    )
    assert projection["next_legal_action"]["ordered_missing_steps_source"] == (
        "deep_successor_contract_state"
    )


def test_completed_deep_successor_prompts_successor_contract_selection():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ]
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}]
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix",
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                157,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                158,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert projection["active_lane_contract"]["role"] == "deep_successor"
    assert projection["active_lane_contract"]["missing_evidence"] == []
    assert projection["next_legal_action"]["id"] == "select_successor_contract"
    assert projection["next_legal_action"]["contract_execution_id"] == (
        "cex-hotfix-r2"
    )
    assert [
        item["contract_template_id"]
        for item in projection["next_legal_action"]["successor_contract_candidates"]
    ] == ["qa_evidence_gate_review.v1"]


def test_deep_successor_followup_qa_binding_exposes_next_lane():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                157,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                158,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                159,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa",
                        "parent_contract_execution_id": "cex-hotfix-r2",
                        "successor_contract_execution_id": "cex-qa-r2",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert [
        item["contract_execution_id"]
        for item in projection["contract_lane_executions"]
    ] == [
        "cex-qa-root",
        "cex-hotfix",
        "cex-qa-followup",
        "cex-hotfix-r2",
        "cex-qa-r2",
    ]
    assert projection["active_lane_contract"]["role"] == "successor_depth_4"
    assert projection["active_lane_contract"]["contract_template_id"] == (
        "qa_evidence_gate_review.v1"
    )
    assert projection["next_legal_action"]["id"] == "qa_review"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa-r2"
    assert projection["next_legal_action"]["ordered_missing_steps_source"] == (
        "successor_depth_4_contract_state.v1"
    )


def test_completed_deep_successor_followup_qa_prompts_close_ready():
    qa_template = {
        "template_id": "qa_evidence_gate_review.v1",
        "evidence_requirements": [
            {"id": "qa_review", "event_kind": "qa_review"},
        ],
        "successor_contract_policy": {
            "selection_required_when_followup_needed": True,
            "candidates": [
                {"contract_template_id": "observer_hotfix_direct_mutation.v1"}
            ],
        },
    }
    hotfix_template = {
        "template_id": "observer_hotfix_direct_mutation.v1",
        "evidence_requirements": [
            {"id": "hotfix_pre_reason", "event_kind": "hotfix_entered"},
            {"id": "hotfix_post_action_summary", "event_kind": "hotfix_under_action"},
        ],
        "successor_contract_policy": {
            "selection_required_after_hotfix_complete": True,
            "candidates": [{"contract_template_id": "qa_evidence_gate_review.v1"}],
        },
    }
    contract = {
        "contract": {
            "contract_id": "qa_evidence_gate_review.v1",
            "contract_template_id": "qa_evidence_gate_review.v1",
            "contract_chain_id": "cchain-qa-hotfix-qa-hotfix-qa-complete",
            "contract_execution_id": "cex-qa-root",
            "state": "selected",
            "evidence_requirements": qa_template["evidence_requirements"],
            "successor_contract_policy": qa_template["successor_contract_policy"],
        }
    }

    projection = build_contract_state_projection(
        [
            _event(150, "qa_review", payload={"contract_execution_id": "cex-qa-root"}),
            _event(
                151,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-qa-root",
                        "successor_contract_execution_id": "cex-hotfix",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                152,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                153,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix"},
            ),
            _event(
                154,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-hotfix",
                        "successor_contract_execution_id": "cex-qa-followup",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(
                155,
                "qa_review",
                payload={"contract_execution_id": "cex-qa-followup"},
            ),
            _event(
                156,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-qa-followup",
                        "successor_contract_execution_id": "cex-hotfix-r2",
                        "contract_template_id": "observer_hotfix_direct_mutation.v1",
                    }
                },
            ),
            _event(
                157,
                "hotfix_entered",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                158,
                "hotfix_under_action",
                payload={"successor_contract_execution_id": "cex-hotfix-r2"},
            ),
            _event(
                159,
                "contract_binding",
                payload={
                    "successor_contract": {
                        "contract_chain_id": (
                            "cchain-qa-hotfix-qa-hotfix-qa-complete"
                        ),
                        "parent_contract_execution_id": "cex-hotfix-r2",
                        "successor_contract_execution_id": "cex-qa-r2",
                        "contract_template_id": "qa_evidence_gate_review.v1",
                    }
                },
            ),
            _event(160, "qa_review", payload={"contract_execution_id": "cex-qa-r2"}),
        ],
        contract=contract,
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        contract_templates={
            "observer_hotfix_direct_mutation.v1": hotfix_template,
            "qa_evidence_gate_review.v1": qa_template,
        },
    )

    assert projection["active_lane_contract"]["role"] == "successor_depth_4"
    assert projection["active_lane_contract"]["missing_evidence"] == []
    assert projection["active_lane_contract"]["next_legal_action"]["id"] == (
        "close_ready"
    )
    assert projection["next_legal_action"]["id"] == "close_ready"
    assert projection["next_legal_action"]["contract_execution_id"] == "cex-qa-r2"


def _strict_worker_identity_contract():
    return {
        "contract": {
            "contract_id": "strict_worker_contract.v1",
            "contract_template_id": "strict_worker_contract.v1",
            "contract_revision_id": "rev-strict-worker",
            "state": "selected",
            "evidence_requirements": [
                {
                    "id": "worker_implementation",
                    "event_kind": "implementation",
                    "expected_writer_role": "mf_sub",
                    "required_route_token_ref": "rtok-expected",
                    "required_runtime_context_id": "mfrctx-expected",
                    "required_task_id": "task-expected",
                    "required_parent_task_id": "parent-expected",
                    "required_worker_slot_id": "worker-slot-expected",
                    "required_fence_token_hash": "sha256:fence-expected",
                }
            ],
        }
    }


def _strict_worker_identity_payload(**overrides):
    payload = {
        "writer_role": "mf_sub",
        "meta_contract_gate": {"role": "mf_sub"},
        "route_token_ref": "rtok-expected",
        "runtime_context_id": "mfrctx-expected",
        "task_id": "task-expected",
        "parent_task_id": "parent-expected",
        "worker_slot_id": "worker-slot-expected",
        "fence_token_hash": "sha256:fence-expected",
    }
    payload.update(overrides)
    return payload


def test_strict_worker_identity_requirement_rejects_payload_self_declared_writer_role():
    payload = _strict_worker_identity_payload(worker_role="mf_sub")
    payload.pop("meta_contract_gate")

    projection = build_contract_state_projection(
        [_event(899, "implementation", payload=payload)],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is False
    assert projection["missing_evidence"] == ["worker_implementation"]
    assert projection["completed_evidence"] == []


def test_strict_worker_identity_requirement_rejects_conflicting_trusted_writer_role():
    projection = build_contract_state_projection(
        [
            _event(
                899,
                "implementation",
                payload=_strict_worker_identity_payload(
                    writer_role="mf_sub",
                    meta_contract_gate={"role": "observer"},
                ),
            )
        ],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is False
    assert projection["missing_evidence"] == ["worker_implementation"]
    assert projection["completed_evidence"] == []


def test_strict_worker_identity_requirement_accepts_matching_event():
    projection = build_contract_state_projection(
        [
            _event(
                900,
                "implementation",
                payload=_strict_worker_identity_payload(),
            )
        ],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    assert projection["contract_complete"] is True
    assert projection["missing_evidence"] == []
    completed = projection["completed_evidence"][0]
    assert completed["id"] == "worker_implementation"
    assert completed["strict_identity"]["writer_role"] == "mf_sub"
    assert completed["strict_identity"]["route_token_ref"] == "rtok-expected"


def test_strict_worker_identity_requirement_rejects_wrong_identity_fields():
    wrong_overrides = {
        "writer_role": {"meta_contract_gate": {"role": "observer"}},
        "route_token_ref": {"route_token_ref": "rtok-wrong"},
        "runtime_context_id": {"runtime_context_id": "mfrctx-wrong"},
        "task_id": {"task_id": "task-wrong"},
        "parent_task_id": {"parent_task_id": "parent-wrong"},
        "worker_slot_id": {"worker_slot_id": "worker-slot-wrong"},
        "fence_token_hash": {"fence_token_hash": "sha256:fence-wrong"},
    }

    for field, overrides in wrong_overrides.items():
        projection = build_contract_state_projection(
            [
                _event(
                    901,
                    "implementation",
                    payload=_strict_worker_identity_payload(**overrides),
                )
            ],
            contract=_strict_worker_identity_contract(),
            backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
        )

        assert projection["contract_complete"] is False, field
        assert projection["missing_evidence"] == ["worker_implementation"], field
        assert projection["completed_evidence"] == [], field


def test_strict_worker_identity_next_action_hint_exposes_expected_fields():
    projection = build_contract_state_projection(
        [],
        contract=_strict_worker_identity_contract(),
        backlog_row={"project_id": "aming-claw", "bug_id": "AC-CONTRACT-RUNTIME"},
    )

    hint = projection["next_legal_action"]["timeline_append_hint"]
    assert hint["event_kind"] == "implementation"
    assert hint["actor_role"] == "mf_sub"
    assert hint["strict_identity"]["match_policy"] == "all_declared_identity_fields"
    assert "meta_contract_gate.role" in hint["strict_identity"][
        "trusted_writer_role_sources"
    ]
    assert "payload.writer_role" not in hint["strict_identity"][
        "trusted_writer_role_sources"
    ]
    assert hint["expected_identity"] == {
        "writer_role": "mf_sub",
        "route_token_ref": "rtok-expected",
        "runtime_context_id": "mfrctx-expected",
        "task_id": "task-expected",
        "parent_task_id": "parent-expected",
        "worker_slot_id": "worker-slot-expected",
        "fence_token_hash": "sha256:fence-expected",
    }
    assert set(hint["required_payload_fields"]) == {
        "writer_role",
        "route_token_ref",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "worker_slot_id",
        "fence_token_hash",
    }
    for field in hint["required_payload_fields"]:
        assert hint["payload"][field] == hint["expected_identity"][field]
