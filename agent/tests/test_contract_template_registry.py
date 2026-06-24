from __future__ import annotations

import json

import pytest

from agent.governance.contract_template_registry import (
    MalformedContractTemplateError,
    UnknownContractTemplateError,
    get_contract_template,
    list_contract_templates,
    resolve_contract_template,
)
from agent.governance.mf_subagent_contract import load_meta_contract_template
from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


def test_template_loading_includes_ue_audit_contract():
    templates = list_contract_templates()
    ids = {template["template_id"] for template in templates}

    assert "ue_audit.v1" in ids
    assert "observer_reminder_echo_demo.v1" in ids


def test_template_filtering_by_task_type_and_stage():
    templates = list_contract_templates(
        task_type="ue_audit",
        stage="pre_frontend_implementation",
    )

    assert [template["template_id"] for template in templates] == ["ue_audit.v1"]


def test_get_template_returns_versioned_source_controlled_template():
    template = get_contract_template("ue_audit.v1")

    assert template["version"] == "v1"
    assert template["source"]["type"] == "source_controlled"
    assert template["expert_profile"]["source_id"] == "aming_claw.bundled_ue_expert.v1"


def test_observer_reminder_echo_template_declares_route_and_worker_echo_requirement():
    template = get_contract_template("observer_reminder_echo_demo.v1")

    assert template["version"] == "v1"
    assert template["service_routes"][0]["service_id"] == "observer.reminder_echo"
    assert template["service_routes"][0]["mode"] == "preview"
    assert template["service_routes"][0]["side_effect_class"] == "read"
    assert template["event_routes"][0]["event_kind"] == "observer.command.notified"
    assert template["event_routes"][0]["service_route_id"] == "service.observer.reminder_echo"
    assert "received_reminder_echo" in template["event_routes"][0]["required_evidence_ids"]
    assert "received_reminder_echo" in template["worker_contract"]["required_fields"]
    assert "received_reminder_echo" in template["worker_contract"]["final_output"]["required_fields"]


def test_mf_workflow_runtime_declares_route_prompt_contract_and_action_gate():
    template = get_contract_template("mf_workflow_runtime.v1")

    service_routes = {
        route["route_id"]: route
        for route in template["service_routes"]
    }
    event_routes = {
        route["route_id"]: route
        for route in template["event_routes"]
    }
    route_action_service = service_routes["service.route.action_precheck"]
    route_action_event = event_routes["event.route_action.pre_mutation"]
    route_action_gate = template["gate_registry"]["route.action.pre_mutation"]

    assert template["route_prompt_contract"]["schema_version"] == (
        "aming_route_prompt_contract.v1"
    )
    assert template["route_prompt_contract"]["required_hash_fields"] == [
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
    ]
    assert service_routes["service.route.prompt_alert_bundle"]["service_id"] == (
        "route.prompt_alert_bundle"
    )
    assert service_routes["service.route.action_precheck"]["service_id"] == (
        "route.action_precheck"
    )
    assert "prompt_contract_hash" in route_action_service["requirement_ids"]
    assert event_routes["event.route_prompt_context.preview"]["service_route_id"] == (
        "service.route.prompt_alert_bundle"
    )
    assert route_action_event["service_route_id"] == "service.route.action_precheck"
    assert "prompt_contract_hash" in route_action_event["required_evidence_ids"]
    assert "prompt_contract_hash_present" in route_action_gate
    assert "selected_topology" in template["route_prompt_contract"]["topology_fields"]
    assert "required_lanes" in template["route_prompt_contract"]["topology_fields"]
    assert "test_files" in template["route_prompt_contract"]["worker_prompt_fields"]
    assert (
        template["route_prompt_contract"]["topology_policy"][
            "p0_p1_governance_routing_runtime_requires_independent_verification"
        ]
        is True
    )
    assert (
        "independent_verification_lane_evidence_for_p0_p1_governance_routing_runtime"
        in template["gate_registry"]["workflow.merge"]
    )
    assert (
        "independent_verification_lane_evidence_for_p0_p1_governance_routing_runtime"
        in template["gate_registry"]["backlog.close"]
    )


def test_mf_workflow_runtime_loads_while_meta_contract_is_present():
    template = get_contract_template("mf_workflow_runtime.v1")
    ids = {item["template_id"] for item in list_contract_templates()}

    assert template["template_id"] == "mf_workflow_runtime.v1"
    assert "mf_workflow_runtime.v1" in ids
    assert "meta_contract.v1" not in ids


def test_review_contract_template_declares_non_close_satisfying_review_events():
    template = get_contract_template("review_contract.v1")

    assert template["contract_kind"] == "review_contract"
    assert template["timeline_contract"]["close_satisfying"] is False
    assert "design_review" in template["timeline_contract"]["allowed_event_kinds"]
    assert template["gate_policy"]["may_satisfy_close_ready"] is False


def test_review_contract_resolves_for_design_review_stage():
    template = resolve_contract_template(
        task_type="design_review",
        stage="design_review",
    )

    assert template["template_id"] == "review_contract.v1"


def test_meta_contract_whitelist_loads_outside_public_template_registry():
    meta_contract = load_meta_contract_template()

    assert meta_contract["id"] == "meta_contract.v1"
    assert "observer" in meta_contract["role_action_whitelist"]
    assert "dispatch_bounded_worker" in (
        meta_contract["role_action_whitelist"]["observer"]["allowed_actions"]
    )
    assert "design_review" in (
        meta_contract["role_action_whitelist"]["observer"]["allowed_actions"]
    )


def test_mf_parallel_template_declares_route_topology_and_worker_prompt_boundary():
    template = get_contract_template("mf_parallel.v1")
    policy = template["route_topology_policy"]
    worker_prompt_contract = template["worker_contract"]["worker_prompt_contract"]

    assert policy["low_risk"]["selected_topology"] == "lightweight_single_lane"
    assert policy["high_risk"]["selected_topology"] == "observer_led_parallel_lanes"
    assert "independent_verification_lane" in policy["high_risk"]["required_lanes"]
    assert "merge" in policy["high_risk"]["observer_authorities"]
    assert "graph_reconcile" in policy["high_risk"]["observer_authorities"]
    assert policy["p0_p1_governance_gate"]["requires_independent_verification_before"] == [
        "workflow.merge",
        "backlog.close",
    ]
    assert "target_files" in worker_prompt_contract["bounded_fields_only"]
    assert "test_files" in worker_prompt_contract["bounded_fields_only"]
    assert "observer_only_context" in worker_prompt_contract["forbidden_context_sources"]


def test_mf_parallel_template_is_discoverable_by_runtime_filters():
    templates = list_contract_templates(task_type="mf_parallel")
    ids = {template["template_id"] for template in templates}

    assert "mf_parallel.v1" in ids
    assert "mf_workflow_runtime.v1" in ids
    assert resolve_contract_template(template_id="mf_parallel.v1", task_type="mf_parallel")[
        "template_id"
    ] == "mf_parallel.v1"


def test_mf_workflow_runtime_template_is_discoverable_by_merge_stage():
    templates = list_contract_templates(stage="merge_preview")
    ids = {template["template_id"] for template in templates}

    assert "mf_workflow_runtime.v1" in ids
    assert "mf_parallel.v1" not in ids
    assert resolve_contract_template(
        template_id="mf_workflow_runtime.v1",
        task_type="mf_parallel",
        stage="merge_preview",
    )["template_id"] == "mf_workflow_runtime.v1"
    assert resolve_contract_template(task_type="mf_parallel", stage="merge_preview")[
        "template_id"
    ] == "mf_workflow_runtime.v1"
    assert resolve_contract_template(task_type="manual_fix", stage="merge_preview")[
        "template_id"
    ] == "mf_workflow_runtime.v1"


def test_observer_hotfix_direct_mutation_template_declares_pre_and_post_timeline_contract():
    template = get_contract_template("observer_hotfix_direct_mutation.v1")
    timeline_contract = template["timeline_contract"]
    pre_event = timeline_contract["pre_mutation_reason_event"]
    post_event = timeline_contract["post_mutation_action_event"]
    evidence_ids = {item["id"] for item in template["evidence_requirements"]}

    assert template["version"] == "v1"
    assert template["source"]["type"] == "source_controlled"
    assert pre_event["event_kind"] == "hotfix_entered"
    assert pre_event["event_type"] == "hotfix.entered"
    assert pre_event["close_satisfying"] is False
    assert "reason" in pre_event["required_payload_fields"]
    assert "allowed_files" in pre_event["required_payload_fields"]
    assert "dirty_scope_before_mutation" in pre_event["required_payload_fields"]
    assert post_event["event_kind"] == "hotfix_under_action"
    assert post_event["event_type"] == "hotfix.under_action"
    assert post_event["close_satisfying"] is False
    assert "pre_reason_event_id" in post_event["required_payload_fields"]
    assert "what_changed" in post_event["required_payload_fields"]
    assert "implementation_close_evidence" in post_event["required_payload_fields"]
    assert timeline_contract["close_gate_evidence_still_required"] == [
        "implementation",
        "verification",
        "close_ready",
    ]
    assert {
        "hotfix_pre_reason",
        "hotfix_post_action_summary",
        "focused_verification",
        "close_ready",
    }.issubset(evidence_ids)
    assert "bypass_timeline_gate" in template["forbidden_capabilities"]


def test_observer_hotfix_direct_mutation_template_resolves_for_pre_mutation_stage():
    templates = list_contract_templates(
        task_type="observer_hotfix",
        stage="pre_mutation",
    )

    assert [template["template_id"] for template in templates] == [
        "observer_hotfix_direct_mutation.v1"
    ]


def test_qa_evidence_gate_template_declares_evidence_and_successors():
    template = get_contract_template("qa_evidence_gate_review.v1")
    evidence_ids = {item["id"] for item in template["evidence_requirements"]}
    successor_ids = {
        item["contract_template_id"]
        for item in template["successor_contract_policy"]["candidates"]
    }

    assert "qa_review" in evidence_ids
    assert "observer_hotfix_direct_mutation.v1" in successor_ids
    assert "audit_close_with_qa_acceptance.v1" in successor_ids


def test_audit_close_with_qa_acceptance_template_declares_no_backfill_policy():
    template = get_contract_template("audit_close_with_qa_acceptance.v1")
    evidence_ids = {item["id"] for item in template["evidence_requirements"]}

    assert template["version"] == "v1"
    assert template["source"]["type"] == "source_controlled"
    assert template["gate_policy"]["normal_close_gate"]["can_close"] is False
    assert template["gate_policy"]["audit_close_gate"]["requires_independent_qa_acceptance"] is True
    assert "qa_acceptance_passed" in evidence_ids
    assert "normal_close_gate_false_preserved" in evidence_ids
    assert "backfill_mf_subagent_startup" in template["forbidden_capabilities"]
    assert "backfill_close_ready" in template["forbidden_capabilities"]


def test_unknown_template_id_raises_explicit_error():
    with pytest.raises(UnknownContractTemplateError):
        get_contract_template("missing.v1")


def test_versioned_resolution_accepts_base_id_plus_version():
    template = resolve_contract_template(template_id="ue_audit", version="v1")

    assert template["template_id"] == "ue_audit.v1"


def test_hotfix_template_aliases_resolve_to_observer_direct_mutation_template():
    for template_id in (
        "hotfix.v1",
        "observer_hotfix.v1",
        "observer_hotfix_direct_mutation.v1",
    ):
        template = resolve_contract_template(
            template_id=template_id,
            task_type="observer_hotfix",
        )

        assert template["template_id"] == "observer_hotfix_direct_mutation.v1"

    template = resolve_contract_template(
        template_id="hotfix",
        task_type="hotfix",
        version="v1",
    )

    assert template["template_id"] == "observer_hotfix_direct_mutation.v1"
    assert get_contract_template("observer_hotfix.v1")["template_id"] == (
        "observer_hotfix_direct_mutation.v1"
    )


def test_resolution_by_task_type_and_stage():
    template = resolve_contract_template(
        task_type="design_review",
        stage="prd_design_review",
    )

    assert template["template_id"] == "ue_audit.v1"


def test_malformed_template_raises_explicit_error(tmp_path):
    (tmp_path / "bad.v1.json").write_text(json.dumps({"schema_version": "x"}), encoding="utf-8")

    with pytest.raises(MalformedContractTemplateError):
        list_contract_templates(template_dir=tmp_path)


def test_schema_json_files_are_not_loaded_as_templates(tmp_path):
    (tmp_path / "review_pack.schema.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema"}),
        encoding="utf-8",
    )
    (tmp_path / "valid.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "test_contract_template.v1",
                "template_id": "valid.v1",
                "version": "v1",
                "task_types": ["task"],
                "stages": ["review_ready"],
            }
        ),
        encoding="utf-8",
    )

    templates = list_contract_templates(template_dir=tmp_path)

    assert [template["template_id"] for template in templates] == ["valid.v1"]


def test_meta_contract_json_file_is_not_loaded_as_public_template(tmp_path):
    (tmp_path / "meta_contract.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "meta_contract.v1",
                "id": "meta_contract.v1",
                "role_action_whitelist": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "valid.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "test_contract_template.v1",
                "template_id": "valid.v1",
                "version": "v1",
                "task_types": ["task"],
                "stages": ["review_ready"],
            }
        ),
        encoding="utf-8",
    )

    templates = list_contract_templates(template_dir=tmp_path)

    assert [template["template_id"] for template in templates] == ["valid.v1"]


def _write_template(tmp_path, payload):
    path = tmp_path / "routes.v1.json"
    base = {
        "schema_version": "test_contract_template.v1",
        "template_id": "routes.v1",
        "task_types": ["task"],
        "stages": ["review_ready"],
    }
    path.write_text(json.dumps({**base, **payload}), encoding="utf-8")
    return path


def _valid_service_route(**extra):
    return {
        "route_id": "service.preview",
        "service_id": "test_governance.preview",
        "mode": "preview",
        "side_effect_class": "read",
        "idempotency_key_policy": {"fields": ["event_id", "event_kind", "route_id"]},
        **extra,
    }


def test_template_validation_accepts_event_and_service_routes(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [_valid_service_route()],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "stage": "review_ready",
                    "service_route_id": "service.preview",
                    "enabled": True,
                }
            ],
        },
    )

    templates = list_contract_templates(template_dir=tmp_path)

    assert templates[0]["event_routes"][0]["route_id"] == "event.task_completed.preview"
    assert templates[0]["service_routes"][0]["service_id"] == "test_governance.preview"


def test_template_validation_accepts_service_routes_as_object(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": {
                "service.preview": {
                    "service_id": "test_governance.preview",
                    "mode": "preview",
                    "side_effect_class": "read",
                    "idempotency_key_policy": {"fields": ["event_id"]},
                }
            },
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    template = list_contract_templates(template_dir=tmp_path)[0]

    assert template["service_routes"][0]["route_id"] == "service.preview"


def test_template_validation_accepts_legacy_side_effect_alias(tmp_path):
    legacy_route = _valid_service_route()
    legacy_route["side_effect"] = legacy_route.pop("side_effect_class")
    _write_template(
        tmp_path,
        {
            "service_routes": [legacy_route],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    template = list_contract_templates(template_dir=tmp_path)[0]

    assert template["service_routes"][0]["side_effect"] == "read"


def test_template_validation_rejects_malformed_route_shape(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [_valid_service_route()],
            "event_routes": "not a list",
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="event_routes must be a list or object"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_invalid_event_route_stages(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [_valid_service_route()],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "stages": [],
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="stages must be a non-empty"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_unknown_service(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [
                _valid_service_route(service_id="missing.service"),
            ],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="unknown service_id"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_ai_route_fields(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [
                _valid_service_route(ai_provider="openai"),
            ],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="forbidden AI field"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_apply_without_permission(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [
                _valid_service_route(
                    route_id="service.cleanup.apply",
                    service_id="cleanup.apply",
                    mode="apply",
                    side_effect_class="write",
                ),
            ],
            "event_routes": [
                {
                    "route_id": "event.cleanup.apply",
                    "event_kind": "cleanup.requested",
                    "service_route_id": "service.cleanup.apply",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="apply/write requires"):
        list_contract_templates(template_dir=tmp_path)


def test_mcp_contract_template_tools_resolve_in_process():
    assert {
        "contract_template_list",
        "contract_template_get",
        "contract_template_resolve",
        "ue_audit_validate",
    }.issubset(_tool_names())

    dispatcher = ToolDispatcher(
        api_fn=lambda method, path, data=None: {"ok": True},
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=lambda method, path, data=None: {"ok": True},
        workspace=".",
    )

    listed = dispatcher.dispatch("contract_template_list", {"task_type": "ue_audit"})
    fetched = dispatcher.dispatch("contract_template_get", {"template_id": "ue_audit.v1"})
    resolved = dispatcher.dispatch(
        "contract_template_resolve",
        {"template_id": "ue_audit", "version": "v1"},
    )
    hotfix_alias = dispatcher.dispatch(
        "contract_template_resolve",
        {"template_id": "hotfix.v1", "task_type": "hotfix"},
    )
    missing = dispatcher.dispatch("contract_template_get", {"template_id": "missing.v1"})

    assert listed["ok"] is True
    assert [template["template_id"] for template in listed["templates"]] == ["ue_audit.v1"]
    assert fetched["template"]["template_id"] == "ue_audit.v1"
    assert resolved["template"]["template_id"] == "ue_audit.v1"
    assert hotfix_alias["template"]["template_id"] == "observer_hotfix_direct_mutation.v1"
    assert missing["ok"] is False
