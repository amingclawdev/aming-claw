from __future__ import annotations

import json

import pytest

from agent.governance.contracts import ContractDefinitionRegistry, ContractRuntime
from agent.governance.contracts.hash import file_sha256
from agent.governance.contracts.runtime import ContractRuntimeError


def _write_minimal_contract(tmp_path, *, status: str = "active"):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    guide = prompts / "observer.md"
    guide.write_text("Follow the compiled runtime guide.\n", encoding="utf-8")
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": "observer_onboard",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "onboard",
        "status": status,
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "bootstrap",
                    "lines": [
                        {
                            "line_id": "read_context",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "evidence_kind": "contract_state_changed",
                        }
                    ],
                },
                {
                    "stage_id": "qa",
                    "lines": [
                        {
                            "line_id": "qa_verdict",
                            "owner_role": "qa",
                            "allowed_writer_roles": ["qa"],
                            "evidence_kind": "qa_verification",
                        }
                    ],
                },
            ]
        },
        "instruction_layer": {
            "inline": ["Runtime guide is authoritative."],
            "refs": [
                {
                    "id": "observer_prompt",
                    "path": "prompts/observer.md",
                    "sha256": file_sha256(guide),
                    "visible_to_roles": ["observer"],
                    "stage_ids": ["bootstrap"],
                }
            ],
        },
    }
    path = tmp_path / "observer_onboard.v1.rev1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_from(record, *, actor_role, stage_id, line_id):
    state = record["execution_state"]
    guide = record["runtime_guide"]
    return {
        "project_id": record["project_id"],
        "backlog_id": record["backlog_id"],
        "contract_execution_id": record["contract_execution_id"],
        "definition_hash": record["definition_hash"],
        "instruction_bundle_hash": record["instruction_bundle_hash"],
        "execution_state_revision": state["execution_state_revision"],
        "runtime_guide_hash": guide["runtime_guide_hash"],
        "stage_id": stage_id,
        "line_id": line_id,
        "actor_role": actor_role,
    }


def test_minimal_contract_runtime_drives_next_action_and_role_gate(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)

    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        contract_execution_id="cex-min-path",
        actor_role="observer",
        route_token_ref="rtok-min-path",
    )

    guide = record["runtime_guide"]
    assert guide["next_legal_action"]["stage_id"] == "bootstrap"
    assert guide["next_legal_action"]["line_id"] == "read_context"
    assert guide["instructions"]["refs"][0]["content"] == "Follow the compiled runtime guide.\n"

    result = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            record,
            actor_role="observer",
            stage_id="bootstrap",
            line_id="read_context",
        ),
    )
    assert result["ok"] is True
    next_record = result["record"]
    assert next_record["execution_state"]["execution_state_revision"] == 2
    assert next_record["runtime_guide"]["next_legal_action"]["stage_id"] == "qa"
    assert next_record["runtime_guide"]["next_legal_action"]["owner_role"] == "qa"

    rejected = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            next_record,
            actor_role="observer",
            stage_id="qa",
            line_id="qa_verdict",
        ),
    )
    assert rejected["ok"] is False
    assert "cannot write line" in rejected["decision"]["errors"][0]

    runtime.current_guide("cex-min-path", actor_role="qa")
    qa_record = runtime.store.get("cex-min-path")
    accepted_qa = runtime.submit_line_write(
        "cex-min-path",
        _write_from(
            qa_record,
            actor_role="qa",
            stage_id="qa",
            line_id="qa_verdict",
        ),
    )
    assert accepted_qa["ok"] is True
    assert accepted_qa["record"]["runtime_guide"]["next_legal_action"] is None


def test_minimal_runtime_rejects_stale_runtime_guide_hash(tmp_path):
    _write_minimal_contract(tmp_path)
    runtime = ContractRuntime(ContractDefinitionRegistry(tmp_path), instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )
    write = _write_from(
        record,
        actor_role="observer",
        stage_id="bootstrap",
        line_id="read_context",
    )
    write["runtime_guide_hash"] = "sha256:" + "0" * 64

    result = runtime.submit_line_write(record["contract_execution_id"], write)

    assert result["ok"] is False
    assert "runtime_guide_hash mismatch" in result["decision"]["errors"]


def test_deprecated_definition_replays_but_cannot_start_new_execution(tmp_path):
    _write_minimal_contract(tmp_path)
    registry = ContractDefinitionRegistry(tmp_path)
    runtime = ContractRuntime(registry, instruction_root=tmp_path)
    record = runtime.start_execution(
        "observer_onboard",
        project_id="aming-claw",
        backlog_id="AC-MIN-PATH",
        actor_role="observer",
    )

    registry.deprecate_definition(
        "observer_onboard",
        version="v1",
        revision="rev1",
        reason="test deprecation",
    )

    with pytest.raises(ContractRuntimeError, match="cannot start new executions"):
        runtime.start_execution(
            "observer_onboard",
            project_id="aming-claw",
            backlog_id="AC-MIN-PATH-2",
            actor_role="observer",
        )

    replay_guide = runtime.current_guide(record["contract_execution_id"])
    assert replay_guide["contract"]["contract_id"] == "observer_onboard"
