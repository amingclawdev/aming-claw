from __future__ import annotations

import json

import pytest

from agent.governance.contracts import ContractDefinitionRegistry, resolve_instruction_bundle
from agent.governance.contracts.hash import file_sha256
from agent.governance.contracts.instructions import InstructionResolutionError


def _payload(expected_hash: str):
    return {
        "schema_version": "contract_definition.v1",
        "contract_id": "qa_onboard",
        "version": "v1",
        "revision": "rev1",
        "role": "qa",
        "contract_type": "qa",
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "review",
                    "lines": [
                        {
                            "line_id": "qa_verdict",
                            "owner_role": "qa",
                            "allowed_writer_roles": ["qa"],
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {
            "inline": ["Verify independently."],
            "refs": [
                {
                    "id": "qa_prompt",
                    "path": "prompts/qa.md",
                    "sha256": expected_hash,
                    "visible_to_roles": ["qa"],
                    "stage_ids": ["review"],
                }
            ],
        },
    }


def test_instruction_bundle_hashes_refs_and_content(tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    prompt = prompt_dir / "qa.md"
    prompt.write_text("Run tests and write QA-owned evidence.\n", encoding="utf-8")
    (tmp_path / "qa.v1.rev1.json").write_text(
        json.dumps(_payload(file_sha256(prompt))),
        encoding="utf-8",
    )
    definition = ContractDefinitionRegistry(tmp_path).get("qa_onboard")

    bundle = resolve_instruction_bundle(definition, root=tmp_path)
    metadata_only = resolve_instruction_bundle(definition, root=tmp_path, include_content=False)

    assert bundle["instruction_bundle_hash"].startswith("sha256:")
    assert bundle["refs"][0]["content"] == "Run tests and write QA-owned evidence.\n"
    assert "content" not in metadata_only["refs"][0]
    assert metadata_only["instruction_bundle_hash"] == bundle["instruction_bundle_hash"]


def test_instruction_bundle_rejects_hash_mismatch(tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "qa.md").write_text("changed\n", encoding="utf-8")
    (tmp_path / "qa.v1.rev1.json").write_text(
        json.dumps(_payload("sha256:" + "1" * 64)),
        encoding="utf-8",
    )
    definition = ContractDefinitionRegistry(tmp_path).get("qa_onboard")

    with pytest.raises(InstructionResolutionError, match="hash mismatch"):
        resolve_instruction_bundle(definition, root=tmp_path)
