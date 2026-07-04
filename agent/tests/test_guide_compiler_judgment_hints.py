from __future__ import annotations

import json

import pytest

from agent.governance.contracts import ContractDefinitionRegistry, ContractRuntime
from agent.governance.contracts.guide_compiler import compile_runtime_guide
from agent.governance.contracts.hash import canonical_json, stable_sha256
from agent.governance.contracts.runtime import _fetch_judgment_hints


def _definition() -> dict:
    return {
        "schema_version": "contract_definition.v1",
        "contract_id": "judgment_hints_contract",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "mf_parallel",
        "definition_hash": "sha256:definition",
    }


def _execution_state(*, actor_role: str = "observer") -> dict:
    return {
        "schema_version": "contract_execution_state.v1",
        "project_id": "aming-claw",
        "backlog_id": "AC-JUDGMENT-HINTS",
        "contract_execution_id": "cex-judgment-hints",
        "execution_state_revision": 1,
        "execution_state_hash": f"sha256:state-{actor_role}",
        "actor_role": actor_role,
        "route_token_ref": "rtok-judgment-hints",
        "next_action": {
            "stage_id": "worker_implementation",
            "line_id": "worker_implementation",
            "owner_role": "mf_sub",
            "allowed_writer_roles": ["mf_sub"],
            "evidence_kind": "implementation",
        },
    }


def _instruction_bundle() -> dict:
    return {
        "instruction_bundle_hash": "sha256:instructions",
        "inline": ["Use the runtime guide."],
        "refs": [],
    }


def _write_contract_definition(tmp_path) -> None:
    payload = {
        "schema_version": "contract_definition.v1",
        "contract_id": "judgment_hints_contract",
        "version": "v1",
        "revision": "rev1",
        "role": "observer",
        "contract_type": "mf_parallel",
        "status": "active",
        "rule_layer": {
            "stages": [
                {
                    "stage_id": "worker_implementation",
                    "lines": [
                        {
                            "line_id": "worker_implementation",
                            "owner_role": "mf_sub",
                            "allowed_writer_roles": ["mf_sub"],
                            "evidence_kind": "implementation",
                        }
                    ],
                }
            ]
        },
        "instruction_layer": {
            "inline": ["Runtime guide is authoritative."],
            "refs": [],
        },
    }
    path = tmp_path / "judgment_hints_contract.v1.rev1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _start_runtime(tmp_path, *, fetcher, contract_execution_id="cex-hints"):
    _write_contract_definition(tmp_path)
    runtime = ContractRuntime(
        ContractDefinitionRegistry(tmp_path),
        instruction_root=tmp_path,
        judgment_hints_fetcher=fetcher,
    )
    record = runtime.start_execution(
        "judgment_hints_contract",
        project_id="aming-claw",
        backlog_id="AC-JUDGMENT-HINTS",
        contract_execution_id=contract_execution_id,
        actor_role="observer",
        route_token_ref="rtok-judgment-hints",
        backlog_lineage={"task_id": "task-judgment-hints"},
    )
    return runtime, record


def _hash_without_runtime_hash(guide: dict) -> str:
    return stable_sha256(
        {key: value for key, value in guide.items() if key != "runtime_guide_hash"}
    )


def test_compile_runtime_guide_v1_byte_identity_for_missing_hints():
    baseline = compile_runtime_guide(
        _definition(),
        _execution_state(),
        instruction_bundle=_instruction_bundle(),
    )
    explicit_none = compile_runtime_guide(
        _definition(),
        _execution_state(),
        instruction_bundle=_instruction_bundle(),
        judgment_hints=None,
    )
    explicit_empty = compile_runtime_guide(
        _definition(),
        _execution_state(),
        instruction_bundle=_instruction_bundle(),
        judgment_hints=[],
    )

    assert canonical_json(explicit_none) == canonical_json(baseline)
    assert canonical_json(explicit_empty) == canonical_json(baseline)
    assert baseline["schema_version"] == "contract_runtime_guide.v1"
    assert "judgment_hints" not in baseline
    assert explicit_none["runtime_guide_hash"] == baseline["runtime_guide_hash"]
    assert explicit_empty["runtime_guide_hash"] == baseline["runtime_guide_hash"]


def test_compile_runtime_guide_v2_seals_hints_into_runtime_hash():
    hints = [
        {
            "source": "judgment-brain",
            "hint": "Keep runtime guide hash deterministic across roles.",
        }
    ]

    guide = compile_runtime_guide(
        _definition(),
        _execution_state(),
        instruction_bundle=_instruction_bundle(),
        judgment_hints=hints,
    )

    assert guide["schema_version"] == "contract_runtime_guide.v2"
    assert guide["judgment_hints"] == hints
    assert guide["runtime_guide_hash"] == _hash_without_runtime_hash(guide)

    tampered = dict(guide)
    tampered["judgment_hints"] = [{"hint": "different"}]
    assert guide["runtime_guide_hash"] != _hash_without_runtime_hash(tampered)


def test_runtime_fetches_once_persists_hints_and_aligns_writer_role_hash(tmp_path):
    calls = []
    hints = [
        {
            "source": "judgment-brain",
            "hint": "Use the writer role runtime guide hash for submit.",
        }
    ]

    def fetcher(**kwargs):
        calls.append(dict(kwargs))
        return {"judgment_hints": hints}

    runtime, record = _start_runtime(tmp_path, fetcher=fetcher)

    assert len(calls) == 1
    assert calls[0]["project_id"] == "aming-claw"
    assert calls[0]["task_id"] == "task-judgment-hints"
    assert record["judgment_hints"] == hints
    assert record["runtime_guide"]["schema_version"] == "contract_runtime_guide.v2"
    assert record["runtime_guide"]["judgment_hints"] == hints

    observer_guide = runtime.current_guide(
        record["contract_execution_id"],
        actor_role="observer",
    )
    copy_payload = observer_guide["writer_role_safe_copy_payload"]["copy_payload"]
    worker_guide = runtime.current_guide(
        record["contract_execution_id"],
        actor_role="mf_sub",
    )

    assert len(calls) == 1
    assert worker_guide["judgment_hints"] == hints
    assert copy_payload["runtime_guide_hash"] == worker_guide["runtime_guide_hash"]

    write = dict(copy_payload)
    write["payload"] = {"schema_version": "test.implementation.v1"}
    result = runtime.submit_line_write(
        record["contract_execution_id"],
        write,
        actor_role="mf_sub",
    )

    assert result["ok"] is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    "fetcher",
    [
        lambda **_kwargs: (_ for _ in ()).throw(OSError("connection refused")),
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
        lambda **_kwargs: (500, '{"judgment_hints":[{"hint":"ignored"}]}'),
        lambda **_kwargs: (200, '{"judgment_hints":['),
    ],
)
def test_judgment_hints_fail_open_cases_keep_v1_envelope(tmp_path, fetcher):
    _, baseline = _start_runtime(
        tmp_path,
        fetcher=lambda **_kwargs: {"judgment_hints": []},
        contract_execution_id="cex-fail-open",
    )
    _, failed_open = _start_runtime(
        tmp_path,
        fetcher=fetcher,
        contract_execution_id="cex-fail-open",
    )

    assert canonical_json(failed_open["runtime_guide"]) == canonical_json(
        baseline["runtime_guide"]
    )
    assert failed_open["runtime_guide"]["schema_version"] == "contract_runtime_guide.v1"
    assert "judgment_hints" not in failed_open["runtime_guide"]


def test_judgment_hints_kill_switch_skips_fetcher(tmp_path, monkeypatch):
    calls = []

    def fetcher(**kwargs):
        calls.append(dict(kwargs))
        raise AssertionError("fetcher must not be called")

    monkeypatch.setenv("AMING_JB_HINTS_DISABLED", "1")
    _, record = _start_runtime(tmp_path, fetcher=fetcher)

    assert calls == []
    assert record["runtime_guide"]["schema_version"] == "contract_runtime_guide.v1"
    assert "judgment_hints" not in record["runtime_guide"]


def test_fetch_judgment_hints_accepts_nonempty_array_payload():
    hints = [{"hint": "persist this"}]

    result = _fetch_judgment_hints(
        project_id="aming-claw",
        task_id="task-judgment-hints",
        fetcher=lambda **_kwargs: (200, json.dumps({"judgment_hints": hints})),
    )

    assert result == hints


def test_onboard_service_refresh_uses_canonical_runtime_guide_hash_recipe():
    from agent.governance import server

    stale_hash = "sha256:" + "0" * 64
    record = {
        "project_id": "aming-claw",
        "backlog_id": "AC-JUDGMENT-HINTS",
        "contract_execution_id": "onboard-service-judgment-hints",
        "route_token_ref": "rtok-judgment-hints",
        "runtime_guide": {
            "schema_version": "onboard_route_guide.runtime_parent.v1",
            "next_legal_action": None,
            "runtime_guide_hash": stale_hash,
        },
    }

    refreshed = server._onboard_service_refresh_execution_state(
        record,
        completed_lines=[
            {
                "stage_id": "onboard_service",
                "line_id": "legacy_onboard_contract_waived",
                "actor_role": "observer",
                "evidence_kind": "onboard_service_waiver",
            }
        ],
        route_token_ref="rtok-judgment-hints",
        revision=2,
    )
    guide = refreshed["runtime_guide"]

    assert guide["runtime_guide_hash"] == _hash_without_runtime_hash(guide)
    assert guide["runtime_guide_hash"] != stable_sha256(
        {**{key: value for key, value in guide.items() if key != "runtime_guide_hash"},
         "runtime_guide_hash": stale_hash}
    )
