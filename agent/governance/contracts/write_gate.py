"""Authoritative line-level write gate for contract executions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .schema import ContractDefinitionError, find_line


@dataclass(frozen=True)
class WriteGateDecision:
    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "contract_write_gate_decision.v1",
            "ok": self.ok,
            "errors": list(self.errors),
        }


def validate_contract_write(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    write: Mapping[str, Any],
    *,
    runtime_guide: Mapping[str, Any] | None = None,
    require_next_action: bool = True,
) -> WriteGateDecision:
    """Validate a proposed evidence/line write against pinned contract state."""

    errors: list[str] = []
    _expect_equal(errors, write, execution_state, "project_id")
    _expect_equal(errors, write, execution_state, "backlog_id")
    _expect_equal(errors, write, execution_state, "contract_execution_id")
    _expect_equal(errors, write, execution_state, "definition_hash")
    _expect_equal(errors, write, execution_state, "instruction_bundle_hash")
    _expect_equal(errors, write, execution_state, "execution_state_revision")

    if runtime_guide is not None:
        _expect_value(
            errors,
            write,
            "runtime_guide_hash",
            runtime_guide.get("runtime_guide_hash"),
        )

    stage_id = str(write.get("stage_id") or "")
    line_id = str(write.get("line_id") or "")
    actor_role = str(write.get("actor_role") or "")
    if not stage_id:
        errors.append("missing stage_id")
    if not line_id:
        errors.append("missing line_id")
    if not actor_role:
        errors.append("missing actor_role")

    line: dict[str, Any] | None = None
    if stage_id and line_id:
        try:
            line = find_line(definition, stage_id=stage_id, line_id=line_id)
        except ContractDefinitionError as exc:
            errors.append(str(exc))
    if line is not None:
        if actor_role:
            allowed = set(str(item) for item in line.get("allowed_writer_roles") or [])
            if actor_role not in allowed:
                errors.append(
                    f"actor_role {actor_role!r} cannot write line {line_id!r}; "
                    f"allowed_writer_roles={sorted(allowed)!r}"
                )
        expected_evidence_kind = str(line.get("evidence_kind") or "")
        write_evidence_kind = str(write.get("evidence_kind") or "")
        if expected_evidence_kind:
            if "evidence_kind" not in write or not write_evidence_kind:
                errors.append("missing evidence_kind")
            elif write_evidence_kind != expected_evidence_kind:
                errors.append("evidence_kind mismatch")

    next_action = execution_state.get("next_action")
    if require_next_action and isinstance(next_action, Mapping):
        expected_stage = str(next_action.get("stage_id") or "")
        expected_line = str(next_action.get("line_id") or "")
        if stage_id != expected_stage or line_id != expected_line:
            errors.append(
                "write does not match next legal action "
                f"{expected_stage!r}/{expected_line!r}"
            )
        _validate_next_action_instance(errors, write, next_action)
    elif require_next_action and next_action is None:
        errors.append("contract execution has no remaining next legal action")

    return WriteGateDecision(ok=not errors, errors=tuple(errors))


def _validate_next_action_instance(
    errors: list[str],
    write: Mapping[str, Any],
    next_action: Mapping[str, Any],
) -> None:
    expected_instance = str(next_action.get("line_instance_id") or "")
    expected_runtime_context_id = str(next_action.get("runtime_context_id") or "")
    expected_task_id = str(next_action.get("task_id") or "")
    expected_lane_id = str(next_action.get("lane_id") or "")
    if not any(
        (expected_instance, expected_runtime_context_id, expected_task_id, expected_lane_id)
    ):
        return

    actual_runtime_context_id = _write_field(write, "runtime_context_id")
    if expected_runtime_context_id:
        if not actual_runtime_context_id:
            errors.append("missing runtime_context_id for next legal action")
        elif actual_runtime_context_id != expected_runtime_context_id:
            errors.append("runtime_context_id does not match next legal action")

    actual_task_id = _write_field(write, "task_id")
    if expected_task_id and actual_task_id and actual_task_id != expected_task_id:
        errors.append("task_id does not match next legal action")

    actual_lane_id = _write_field(write, "lane_id", "worker_slot_id", "worker_id")
    if expected_lane_id and actual_lane_id and actual_lane_id != expected_lane_id:
        errors.append("lane_id does not match next legal action")

    actual_instance = _write_field(write, "line_instance_id", "instance_id")
    if not actual_instance:
        actual_instance = _line_instance_id_from_values(
            runtime_context_id=actual_runtime_context_id,
            task_id=actual_task_id,
            lane_id=actual_lane_id,
        )
    if expected_instance and actual_instance and actual_instance != expected_instance:
        errors.append("line_instance_id does not match next legal action")
    elif expected_instance and not actual_instance:
        errors.append("missing line_instance_id for next legal action")


def _write_field(write: Mapping[str, Any], *keys: str) -> str:
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    for source in (write, payload):
        for key in keys:
            value = source.get(key)
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _line_instance_id_from_values(
    *,
    runtime_context_id: str = "",
    task_id: str = "",
    lane_id: str = "",
) -> str:
    if runtime_context_id:
        return f"runtime_context:{runtime_context_id}"
    if task_id:
        return f"task:{task_id}"
    if lane_id:
        return f"lane:{lane_id}"
    return ""


def _expect_equal(
    errors: list[str],
    write: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    field: str,
) -> None:
    _expect_value(errors, write, field, execution_state.get(field))


def _expect_value(
    errors: list[str],
    write: Mapping[str, Any],
    field: str,
    expected: Any,
) -> None:
    if field not in write:
        errors.append(f"missing {field}")
        return
    if write.get(field) != expected:
        errors.append(f"{field} mismatch")
