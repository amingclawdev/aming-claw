from __future__ import annotations

from pathlib import Path

import pytest

from agent.governance.contracts import ContractCrudService, ContractDefinitionRegistry


def _definition(**overrides):
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": "observer_hotfix",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "implementation",
        "status": "active",
        "compat_aliases": ["observer_hotfix_direct_mutation.v1"],
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "pre_mutation",
                    "lines": [
                        {
                            "line_id": "reason",
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
                            "line_id": "independent_qa",
                            "owner_role": "qa",
                            "allowed_writer_roles": ["qa"],
                            "evidence_kind": "qa_verification",
                        }
                    ],
                },
            ]
        },
        "instruction_layer": {"inline": ["Use runtime guide only."], "refs": []},
        "successors": [{"contract_id": "qa_onboard", "version": "v1"}],
    }
    payload.update(overrides)
    return payload


def test_crud_service_lists_reads_and_validates_definitions(tmp_path: Path):
    service = ContractCrudService(tmp_path)

    empty = service.list()
    assert empty["ok"] is True
    assert empty["data"] == {"definitions": [], "count": 0}

    validated = service.validate(_definition())
    assert validated["ok"] is True
    assert validated["operation"] == "validate"
    assert validated["data"]["definition"]["definition_hash"].startswith("sha256:")

    created = service.create(_definition())
    assert created["ok"] is True
    assert created["status"] == "created"
    assert Path(created["data"]["path"]).exists()

    listed = service.list()
    assert listed["data"]["count"] == 1
    assert listed["data"]["definitions"][0]["contract_id"] == "observer_hotfix"

    by_alias = service.read("observer_hotfix_direct_mutation.v1")
    assert by_alias["ok"] is True
    assert by_alias["data"]["definition"]["contract_id"] == "observer_hotfix"


def test_crud_service_preserves_registry_lifecycle_rules(tmp_path: Path):
    service = ContractCrudService(tmp_path)
    created = service.create(_definition())
    existing_hash = created["data"]["definition"]["definition_hash"]

    changed_same_revision = _definition(
        rule_layer={
            "stages": [
                {
                    "stage_id": "pre_mutation",
                    "lines": [
                        {
                            "line_id": "reason",
                            "owner_role": "observer",
                            "allowed_writer_roles": ["observer"],
                            "description": "semantic change",
                        }
                    ],
                }
            ]
        }
    )
    blocked_update = service.update(
        changed_same_revision,
        expected_previous_hash=existing_hash,
    )
    assert blocked_update["ok"] is False
    assert blocked_update["error"]["type"] == "ContractLifecycleError"
    assert "new revision" in blocked_update["error"]["message"]

    updated = service.update(_definition(revision="rev2"))
    assert updated["ok"] is True
    assert updated["data"]["definition"]["revision"] == "rev2"

    deprecated = service.deprecate(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        reason="replaced by rev2",
    )
    assert deprecated["ok"] is True
    assert deprecated["data"]["definition"]["status"] == "deprecated"

    active_only = service.list(include_deprecated=False)
    assert [item["revision"] for item in active_only["data"]["definitions"]] == ["rev2"]

    blocked_delete = service.hard_delete(
        "observer_hotfix",
        version="v1",
        revision="rev1",
        references=["timeline:1"],
    )
    assert blocked_delete["ok"] is False
    assert blocked_delete["error"]["type"] == "ContractLifecycleError"
    assert "hard delete" in blocked_delete["error"]["message"]

    draft = service.create(_definition(revision="rev3", status="draft"))
    deleted = service.hard_delete(
        "observer_hotfix",
        version="v1",
        revision="rev3",
        references=["draft-ref-ok"],
    )
    assert deleted["ok"] is True
    assert deleted["data"]["deleted"] == {
        "contract_id": "observer_hotfix",
        "version": "v1",
        "revision": "rev3",
        "status": "draft",
    }
    assert not Path(draft["data"]["path"]).exists()


def test_crud_service_returns_structured_failure_results(tmp_path: Path):
    service = ContractCrudService(tmp_path)

    missing = service.read("missing")
    assert missing == {
        "ok": False,
        "operation": "read",
        "status": "failed",
        "data": {},
        "error": {
            "type": "UnknownContractDefinitionError",
            "message": "unknown contract definition: missing",
        },
    }

    invalid = service.validate({"schema_version": "contract_definition.v1"})
    assert invalid["ok"] is False
    assert invalid["operation"] == "validate"
    assert invalid["error"]["type"] == "ContractDefinitionError"
    assert "contract_id" in invalid["error"]["message"]

    assert service.create(_definition())["ok"] is True
    duplicate = service.create(_definition())
    assert duplicate["ok"] is False
    assert duplicate["operation"] == "create"
    assert duplicate["error"]["type"] == "ContractLifecycleError"


def test_crud_service_accepts_registry_injection(tmp_path: Path):
    registry = ContractDefinitionRegistry(tmp_path)
    service = ContractCrudService(registry=registry)

    assert service.create(_definition())["ok"] is True
    assert service.read("observer_hotfix")["ok"] is True

    with pytest.raises(ValueError, match="either root or registry"):
        ContractCrudService(tmp_path, registry=registry)
