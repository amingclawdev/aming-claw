from __future__ import annotations

from pathlib import Path

import pytest

from agent.governance.review_contracts import (
    UnknownReviewPackError,
    get_review_pack,
    list_review_packs,
    resolve_review_pack,
    validate_review_output,
)
from agent.mcp.tools import TOOLS, ToolDispatcher


EXPECTED_PACK_IDS = {
    "architecture_data_continuity_review.v1",
    "frontend_ui_implementation_review.v1",
    "qa_evidence_gate_review.v1",
}


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


def _valid_output() -> dict:
    return {
        "template_id": "qa_evidence_gate_review.v1",
        "gate_decision": "pass_with_followups",
        "findings": [
            {
                "finding_id": "qa-001",
                "severity": "minor",
                "dimension": "fixture_integrity",
                "summary": "Fixture cleanup should be named in close evidence.",
                "evidence_refs": ["pytest:agent/tests/test_review_contract_packs.py"],
                "recommendation": "Add the fixture cleanup command to close evidence.",
                "acceptance_impact": "Adds an explicit verification artifact.",
                "backlog_conversion_hints": [
                    {
                        "target": "acceptance_criteria",
                        "action": "follow_up_backlog",
                        "acceptance_impact": "Track cleanup evidence before close.",
                    }
                ],
            }
        ],
    }


def test_review_pack_loading_returns_development_packs():
    ids = {pack["template_id"] for pack in list_review_packs()}

    assert EXPECTED_PACK_IDS.issubset(ids)


def test_review_pack_get_normalizes_top_level_output_fields():
    pack = get_review_pack("architecture_data_continuity_review.v1")

    assert pack["schema_version"] == "review_pack.v1"
    assert pack["contract_kind"] == "review_pack"
    assert pack["required_output"]["gate_decisions"] == [
        "pass",
        "pass_with_followups",
        "block",
    ]
    assert "backlog_conversion_hints" in pack["required_output"]["finding_fields"]


def test_review_pack_resolve_by_task_type_stage_and_version():
    pack = resolve_review_pack(
        task_type="evidence_gate",
        stage="pre_close_gate",
        version="v1",
    )

    assert pack["template_id"] == "qa_evidence_gate_review.v1"


def test_review_pack_resolution_fails_loudly_on_unknown_or_version_mismatch():
    with pytest.raises(UnknownReviewPackError):
        resolve_review_pack(template_id="qa_evidence_gate_review", version="v2")


def test_validate_review_output_accepts_backlog_conversion_hint_path():
    result = validate_review_output("qa_evidence_gate_review.v1", _valid_output())

    assert result["ok"] is True
    assert result["template_id"] == "qa_evidence_gate_review.v1"
    assert result["backlog_conversion_actions"] == [
        {
            "finding_index": 0,
            "hint_index": 0,
            "target": "acceptance_criteria",
            "action": "follow_up_backlog",
            "acceptance_impact": "Track cleanup evidence before close.",
        }
    ]


def test_validate_review_output_rejects_invalid_gate_and_severity():
    payload = _valid_output()
    payload["gate_decision"] = "auto_approve"
    payload["findings"][0]["severity"] = "cosmetic"

    result = validate_review_output("qa_evidence_gate_review.v1", payload)

    assert result["ok"] is False
    assert "gate_decision must be one of: pass, pass_with_followups, block" in result["errors"]
    assert "findings[0].severity must be one of: info, minor, major, critical" in result["errors"]


def test_validate_review_output_rejects_missing_evidence_and_acceptance_impact():
    payload = _valid_output()
    payload["findings"][0]["evidence_refs"] = []
    payload["findings"][0].pop("acceptance_impact")

    result = validate_review_output("qa_evidence_gate_review.v1", payload)

    assert result["ok"] is False
    assert "findings[0].evidence_refs must be a non-empty list of strings" in result["errors"]
    assert "findings[0] missing required field: acceptance_impact" in result["errors"]


def test_validate_review_output_rejects_malformed_backlog_conversion_hints():
    payload = _valid_output()
    payload["findings"][0]["backlog_conversion_hints"] = [
        {"target": "acceptance_criteria", "action": "not_allowed"}
    ]

    result = validate_review_output("qa_evidence_gate_review.v1", payload)

    assert result["ok"] is False
    assert (
        "findings[0].backlog_conversion_hints[0].action must be one of: "
        "none, acceptance_criteria, follow_up_backlog"
    ) in result["errors"]
    assert "findings[0].backlog_conversion_hints[0] must declare acceptance_impact" in result["errors"]


def test_mcp_review_pack_tools_resolve_and_validate_in_process():
    assert {
        "review_pack_list",
        "review_pack_get",
        "review_pack_resolve",
        "review_pack_validate_output",
    }.issubset(_tool_names())

    dispatcher = ToolDispatcher(
        api_fn=lambda method, path, data=None: {"ok": True},
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=lambda method, path, data=None: {"ok": True},
        workspace=".",
    )

    listed = dispatcher.dispatch("review_pack_list", {"task_type": "evidence_gate"})
    fetched = dispatcher.dispatch("review_pack_get", {"template_id": "qa_evidence_gate_review.v1"})
    resolved = dispatcher.dispatch(
        "review_pack_resolve",
        {"template_id": "qa_evidence_gate_review", "version": "v1"},
    )
    validated = dispatcher.dispatch(
        "review_pack_validate_output",
        {"template_id": "qa_evidence_gate_review.v1", "payload": _valid_output()},
    )
    missing = dispatcher.dispatch("review_pack_get", {"template_id": "missing.v1"})

    assert listed["ok"] is True
    assert [pack["template_id"] for pack in listed["review_packs"]] == [
        "qa_evidence_gate_review.v1"
    ]
    assert fetched["review_pack"]["template_id"] == "qa_evidence_gate_review.v1"
    assert resolved["review_pack"]["template_id"] == "qa_evidence_gate_review.v1"
    assert validated["ok"] is True
    assert missing["ok"] is False


def test_review_pack_sources_are_provider_neutral():
    agent_root = Path(__file__).resolve().parents[1]
    touched_paths = [
        agent_root / "governance" / "review_contracts.py",
        agent_root / "governance" / "contract_templates" / "architecture_data_continuity_review.v1.json",
        agent_root / "governance" / "contract_templates" / "frontend_ui_implementation_review.v1.json",
        agent_root / "governance" / "contract_templates" / "qa_evidence_gate_review.v1.json",
    ]
    forbidden_markers = {
        "provider" + "_specific_tool_name",
        "raw" + "_private_memory",
        "candidate" + "_scoring_internals",
        "private" + "_final_decision_lane",
    }
    combined = "\n".join(path.read_text(encoding="utf-8") for path in touched_paths)

    assert all(marker not in combined for marker in forbidden_markers)
