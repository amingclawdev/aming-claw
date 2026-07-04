"""Authoritative line-level write gate for contract executions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .schema import ContractDefinitionError, find_line


_DIRECT_FIX_GRAPH_CONTEXT_POLICIES = {
    "direct_fix_observer_graph_scope": {
        "actor_role": "observer",
        "query_sources": {"observer"},
        "query_purposes": {
            "observer_scope_build",
            "observer_scope_validation",
            "graph_scope_before_dispatch",
        },
        "required_identity_fields": ("target_project_root",),
    },
    "direct_fix_worker_graph_context": {
        "actor_role": "mf_sub",
        "query_sources": {"mf_subagent"},
        "query_purposes": {
            "subagent_context_build",
            "subagent_gate_validation",
            "subagent_scope_validation",
        },
        "required_identity_fields": (
            "runtime_context_id",
            "task_id",
            "parent_task_id",
            "target_project_root",
        ),
        "worker_role": "mf_sub",
    },
    "direct_fix_qa_graph_context": {
        "actor_role": "qa",
        "query_sources": {"qa"},
        "query_purposes": {
            "qa_context_build",
            "qa_gate_validation",
            "independent_verification",
        },
        "required_identity_fields": ("target_project_root",),
    },
}

_GRAPH_TRACE_ID_KEYS = {
    "graph_trace_id",
    "graph_trace_ids",
    "trace_id",
    "trace_ids",
    "verified_trace_ids",
}


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
        _expect_runtime_guide_hash(errors, write, runtime_guide)

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

    _validate_direct_fix_graph_context(errors, write, line_id=line_id, actor_role=actor_role)

    return WriteGateDecision(ok=not errors, errors=tuple(errors))


def _validate_direct_fix_graph_context(
    errors: list[str],
    write: Mapping[str, Any],
    *,
    line_id: str,
    actor_role: str,
) -> None:
    policy = _DIRECT_FIX_GRAPH_CONTEXT_POLICIES.get(line_id)
    if not policy:
        return
    expected_actor = str(policy.get("actor_role") or "")
    if actor_role != expected_actor:
        errors.append(f"{line_id} requires actor_role={expected_actor}")

    trace_ids = _graph_trace_ids(write)
    if not trace_ids:
        errors.append(f"{line_id} requires non-empty graph_trace_ids")
    elif not all(_is_plausible_graph_trace_id(trace_id) for trace_id in trace_ids):
        errors.append(f"{line_id} contains invalid graph_trace_ids")

    db_verified = _graph_bool(write, "db_verified")
    if not db_verified:
        errors.append(f"{line_id} requires db_verified graph_trace_evidence")

    query_source = _graph_text(write, "query_source")
    allowed_sources = set(policy.get("query_sources") or [])
    if not query_source:
        errors.append(f"{line_id} requires graph query_source")
    elif query_source not in allowed_sources:
        errors.append(f"{line_id} query_source must be one of {sorted(allowed_sources)!r}")

    query_purpose = _graph_text(write, "query_purpose")
    allowed_purposes = set(policy.get("query_purposes") or [])
    if not query_purpose:
        errors.append(f"{line_id} requires graph query_purpose")
    elif query_purpose not in allowed_purposes:
        errors.append(f"{line_id} query_purpose must be one of {sorted(allowed_purposes)!r}")

    expected_worker_role = str(policy.get("worker_role") or "")
    if expected_worker_role:
        worker_role = _graph_text(write, "worker_role")
        if worker_role != expected_worker_role:
            errors.append(f"{line_id} requires worker_role={expected_worker_role}")

    for field in policy.get("required_identity_fields") or ():
        write_value = _write_field(write, field)
        graph_value = _graph_text(write, field)
        value = write_value or graph_value
        if not value:
            errors.append(f"{line_id} requires {field}")
            continue
        if write_value and graph_value and write_value != graph_value:
            errors.append(f"{line_id} {field} does not match graph_trace_evidence")


def _graph_trace_ids(write: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _GRAPH_TRACE_ID_KEYS:
        values.extend(_flatten_graph_text_values(write.get(key)))
    for candidate in _graph_evidence_candidates(write):
        for key in _GRAPH_TRACE_ID_KEYS:
            values.extend(_flatten_graph_text_values(candidate.get(key)))
    return list(dict.fromkeys(value for value in values if value))


def _graph_text(write: Mapping[str, Any], key: str) -> str:
    value = _write_field(write, key)
    if value:
        return value
    for candidate in _graph_evidence_candidates(write):
        value = _first_deep_text(candidate, key)
        if value:
            return value
    return ""


def _graph_bool(write: Mapping[str, Any], key: str) -> bool:
    for candidate in _graph_evidence_candidates(write):
        value = _first_deep_value(candidate, key)
        if isinstance(value, bool):
            return value
        if str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _graph_evidence_candidates(write: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    payload = write.get("payload") if isinstance(write.get("payload"), Mapping) else {}
    for source in (write, payload):
        for key in (
            "graph_trace_evidence",
            "graph_trace_db_evidence",
            "graph_trace",
            "graph_context",
        ):
            value = source.get(key)
            if isinstance(value, Mapping):
                candidates.append(value)
        if any(key in source for key in ("query_source", "query_purpose", "db_verified")):
            candidates.append(source)
    return candidates


def _flatten_graph_text_values(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        values: list[str] = []
        for child in value.values():
            values.extend(_flatten_graph_text_values(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_flatten_graph_text_values(child))
        return values
    text = str(value or "").strip()
    return [text] if text else []


def _first_deep_text(value: Any, key: str) -> str:
    item = _first_deep_value(value, key)
    return str(item or "").strip()


def _first_deep_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if str(raw_key or "") == key:
                return child
            found = _first_deep_value(child, key)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_deep_value(child, key)
            if found not in (None, ""):
                return found
    return None


def _is_plausible_graph_trace_id(value: str) -> bool:
    return value.startswith("gqt-") and len(value) >= 8


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


def _expect_runtime_guide_hash(
    errors: list[str],
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
) -> None:
    field = "runtime_guide_hash"
    expected = runtime_guide.get(field)
    if field not in write:
        errors.append(f"missing {field}")
        return
    actual = write.get(field)
    if actual == expected:
        return
    errors.append(_runtime_guide_hash_mismatch_message(write, runtime_guide, actual, expected))


def _runtime_guide_hash_mismatch_message(
    write: Mapping[str, Any],
    runtime_guide: Mapping[str, Any],
    actual: Any,
    expected: Any,
) -> str:
    copy_payload = _mapping(runtime_guide.get("writer_role_safe_copy_payload"))
    alignment = _mapping(copy_payload.get("hash_alignment"))
    submit_payload = _mapping(copy_payload.get("copy_payload"))
    actor_role = str(write.get("actor_role") or submit_payload.get("actor_role") or "")
    required_role = str(
        alignment.get("required_writer_role")
        or alignment.get("required_owner_role")
        or actor_role
    )
    required_hash = str(
        alignment.get("required_writer_runtime_guide_hash")
        or submit_payload.get("runtime_guide_hash")
        or expected
        or ""
    )
    actual_hash = str(actual or "")
    reader_role = _matching_reader_role(alignment, actual_hash, required_role)
    if not reader_role:
        reader_role = str(alignment.get("reader_role") or "")
    reader_fragment = (
        f"received reader-role guide hash for role {reader_role!r} ({actual_hash})"
        if reader_role and actual_hash
        else f"received {actual_hash!r}, which is not the required owner/writer-role hash"
    )
    return (
        "runtime_guide_hash mismatch: "
        f"{reader_fragment}; submit_line requires owner/writer-role guide hash "
        f"for role {required_role!r} ({required_hash}). "
        "Recover by copying writer_role_safe_copy_payload.copy_payload.runtime_guide_hash "
        "or the full writer_role_safe_copy_payload.copy_payload from the current guide "
        "before calling contract_runtime_submit_line."
    )


def _matching_reader_role(
    alignment: Mapping[str, Any],
    actual_hash: str,
    required_role: str,
) -> str:
    for item in alignment.get("known_role_runtime_guide_hashes") or []:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        if role == required_role:
            continue
        if str(item.get("runtime_guide_hash") or "") == actual_hash:
            return role
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
