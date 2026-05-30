"""Public-safe review contract pack registry and output validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent.governance.contract_template_registry import (
    DEFAULT_TEMPLATE_DIR,
    ContractTemplateError,
    UnknownContractTemplateError,
    get_contract_template,
    list_contract_templates,
)


REVIEW_PACK_SCHEMA_VERSION = "review_pack.v1"
REVIEW_PACK_KIND = "expert_review_pack"
BACKLOG_HINT_TARGET_ALIASES = ("target", "conversion_target", "backlog_target")
BACKLOG_HINT_ACTION_ALIASES = ("action", "conversion_action")
BACKLOG_HINT_IMPACT_ALIASES = (
    "acceptance_impact",
    "acceptance_criteria_impact",
    "impact",
)


class ReviewContractError(ValueError):
    """Base error for review contract pack failures."""


class UnknownReviewPackError(ReviewContractError):
    """Raised when no review pack matches a requested key."""


class MalformedReviewPackError(ReviewContractError):
    """Raised when a review pack is not usable."""


def list_review_packs(
    task_type: str | None = None,
    stage: str | None = None,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> list[dict[str, Any]]:
    """List source-controlled review packs, optionally filtered by task type and stage."""

    packs: list[dict[str, Any]] = []
    for template in list_contract_templates(
        template_dir=template_dir,
        task_type=task_type,
        stage=stage,
    ):
        if _is_review_pack(template):
            packs.append(_validate_review_pack(template))
    return sorted(packs, key=lambda item: str(item["template_id"]))


def get_review_pack(
    template_id: str,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Return a review pack by exact versioned template id."""

    try:
        template = get_contract_template(template_id, template_dir=template_dir)
    except UnknownContractTemplateError as exc:
        raise UnknownReviewPackError(f"unknown review pack: {template_id}") from exc
    except ContractTemplateError as exc:
        raise MalformedReviewPackError(str(exc)) from exc
    if not _is_review_pack(template):
        raise UnknownReviewPackError(f"contract template is not a review pack: {template_id}")
    return _validate_review_pack(template)


def resolve_review_pack(
    template_id: str | None = None,
    task_type: str | None = None,
    stage: str | None = None,
    version: str | None = None,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Resolve one review pack by id, version, task type, and stage."""

    packs = list_review_packs(task_type=task_type, stage=stage, template_dir=template_dir)
    if template_id:
        packs = [
            pack
            for pack in packs
            if pack["template_id"] == template_id
            or (version and pack["template_id"] == f"{template_id}.{version}")
        ]
    if version:
        packs = [pack for pack in packs if pack.get("version") == version]
    if not packs:
        key = ", ".join(
            part
            for part in (
                f"template_id={template_id}" if template_id else "",
                f"task_type={task_type}" if task_type else "",
                f"stage={stage}" if stage else "",
                f"version={version}" if version else "",
            )
            if part
        )
        raise UnknownReviewPackError(f"unknown review pack resolution: {key or 'empty query'}")
    if len(packs) > 1:
        raise ReviewContractError(
            "ambiguous review pack resolution: "
            + ", ".join(str(pack["template_id"]) for pack in packs)
        )
    return packs[0]


def validate_review_output(
    template_id: str,
    payload: Mapping[str, Any],
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Validate a machine-readable review output against a review pack."""

    if not isinstance(template_id, str) or not template_id:
        return {"ok": False, "errors": ["template_id is required"], "template_id": ""}
    if not isinstance(payload, Mapping):
        return {"ok": False, "errors": ["payload must be an object"], "template_id": template_id}

    try:
        pack = get_review_pack(template_id, template_dir=template_dir)
    except ReviewContractError as exc:
        return {"ok": False, "errors": [str(exc)], "template_id": template_id}

    errors: list[str] = []
    actions: list[dict[str, Any]] = []
    required_output = _required_output(pack)
    gate_decisions = _string_values(required_output.get("gate_decisions"))
    severity_values = _string_values(required_output.get("severity_values"))
    finding_fields = _string_values(required_output.get("finding_fields"))

    payload_template_id = payload.get("template_id")
    if payload_template_id is not None and payload_template_id != pack["template_id"]:
        errors.append(f"template_id must be {pack['template_id']}")

    gate_decision = payload.get("gate_decision")
    if gate_decision not in gate_decisions:
        errors.append("gate_decision must be one of: " + ", ".join(gate_decisions))

    findings = payload.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
    else:
        for index, finding in enumerate(findings):
            if not isinstance(finding, Mapping):
                errors.append(f"findings[{index}] must be an object")
                continue
            _validate_finding(
                finding,
                index=index,
                required_fields=finding_fields,
                severity_values=severity_values,
                pack=pack,
                errors=errors,
                actions=actions,
            )

    return {
        "ok": not errors,
        "errors": errors,
        "template_id": pack["template_id"],
        "backlog_conversion_actions": actions,
    }


def _is_review_pack(template: Mapping[str, Any]) -> bool:
    review_pack = template.get("review_pack")
    has_kind = isinstance(review_pack, Mapping) and review_pack.get("kind") == REVIEW_PACK_KIND
    return has_kind or template.get("schema_version") == REVIEW_PACK_SCHEMA_VERSION


def _validate_review_pack(template: Mapping[str, Any]) -> dict[str, Any]:
    file_name = _source_path(template)
    if not _is_review_pack(template):
        raise MalformedReviewPackError(
            f"{file_name}: review_pack.kind must be {REVIEW_PACK_KIND!r} "
            f"or schema_version must be {REVIEW_PACK_SCHEMA_VERSION!r}"
        )

    required_inputs = _required_string_list(template, "required_inputs", file_name=file_name)
    forbidden_assumptions = _required_string_list(
        template,
        "forbidden_assumptions",
        file_name=file_name,
    )
    review_dimensions = _required_string_list(template, "review_dimensions", file_name=file_name)

    artifact_refs = template.get("artifact_refs")
    if not isinstance(artifact_refs, list) or not artifact_refs:
        raise MalformedReviewPackError(f"{file_name}: artifact_refs must be a non-empty list")
    for index, item in enumerate(artifact_refs):
        if isinstance(item, str) and item:
            continue
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item.get("id"):
            continue
        raise MalformedReviewPackError(f"{file_name}: artifact_refs[{index}] must declare an id")

    required_output = _required_output(template)
    gate_decisions = _required_string_list(required_output, "gate_decisions", file_name=file_name)
    severity_values = _required_string_list(required_output, "severity_values", file_name=file_name)
    finding_fields = _required_string_list(required_output, "finding_fields", file_name=file_name)
    for field in ("severity", "evidence_refs", "acceptance_impact", "backlog_conversion_hints"):
        if field not in finding_fields:
            raise MalformedReviewPackError(f"{file_name}: finding_fields must include {field}")

    backlog_conversion_hints = template.get("backlog_conversion_hints")
    if not isinstance(backlog_conversion_hints, Mapping):
        raise MalformedReviewPackError(f"{file_name}: backlog_conversion_hints must be an object")
    allowed_actions = _string_values(backlog_conversion_hints.get("allowed_actions"))
    if not allowed_actions:
        raise MalformedReviewPackError(
            f"{file_name}: backlog_conversion_hints.allowed_actions must be a non-empty list"
        )

    normalized = dict(template)
    normalized["required_inputs"] = required_inputs
    normalized["forbidden_assumptions"] = forbidden_assumptions
    normalized["review_dimensions"] = review_dimensions
    normalized["required_output"] = {
        **dict(required_output),
        "gate_decisions": gate_decisions,
        "severity_values": severity_values,
        "finding_fields": finding_fields,
    }
    return normalized


def _required_output(template: Mapping[str, Any]) -> Mapping[str, Any]:
    required_output = template.get("required_output")
    if isinstance(required_output, Mapping):
        return required_output
    top_level = {
        "gate_decisions": template.get("gate_decisions"),
        "severity_values": template.get("severity_values"),
        "finding_fields": template.get("finding_fields"),
    }
    if all(isinstance(value, list) for value in top_level.values()):
        output_schema = template.get("output_schema")
        if isinstance(output_schema, Mapping):
            top_level["schema"] = output_schema
        return top_level
    raise MalformedReviewPackError(
        f"{_source_path(template)}: required_output or top-level output fields must be declared"
    )


def _validate_finding(
    finding: Mapping[str, Any],
    *,
    index: int,
    required_fields: Iterable[str],
    severity_values: list[str],
    pack: Mapping[str, Any],
    errors: list[str],
    actions: list[dict[str, Any]],
) -> None:
    for field in required_fields:
        if _is_missing(finding.get(field)):
            errors.append(f"findings[{index}] missing required field: {field}")

    severity = finding.get("severity")
    if not _is_missing(severity) and severity not in severity_values:
        errors.append(f"findings[{index}].severity must be one of: " + ", ".join(severity_values))

    _require_non_empty_string_list(
        finding.get("evidence_refs"),
        errors,
        label=f"findings[{index}].evidence_refs",
    )
    if _is_missing(finding.get("acceptance_impact")):
        errors.append(f"findings[{index}].acceptance_impact must be a non-empty string")
    elif not isinstance(finding.get("acceptance_impact"), str):
        errors.append(f"findings[{index}].acceptance_impact must be a string")

    _validate_backlog_conversion_hints(
        finding.get("backlog_conversion_hints"),
        index=index,
        pack=pack,
        errors=errors,
        actions=actions,
    )


def _validate_backlog_conversion_hints(
    value: Any,
    *,
    index: int,
    pack: Mapping[str, Any],
    errors: list[str],
    actions: list[dict[str, Any]],
) -> None:
    label = f"findings[{index}].backlog_conversion_hints"
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return

    pack_hints = pack.get("backlog_conversion_hints")
    if not isinstance(pack_hints, Mapping):
        pack_hints = {}
    allowed_actions = _string_values(pack_hints.get("allowed_actions"))
    allowed_targets = _string_values(pack_hints.get("targets"))

    for hint_index, hint in enumerate(value):
        hint_label = f"{label}[{hint_index}]"
        if not isinstance(hint, Mapping):
            errors.append(f"{hint_label} must be an object")
            continue
        target = _aliased_str(hint, BACKLOG_HINT_TARGET_ALIASES)
        action = _aliased_str(hint, BACKLOG_HINT_ACTION_ALIASES)
        acceptance_impact = _aliased_str(hint, BACKLOG_HINT_IMPACT_ALIASES)
        if not target:
            errors.append(f"{hint_label} must declare target")
        elif allowed_targets and target not in allowed_targets:
            errors.append(f"{hint_label}.target must be one of: " + ", ".join(allowed_targets))
        if not action:
            errors.append(f"{hint_label} must declare action")
        elif allowed_actions and action not in allowed_actions:
            errors.append(f"{hint_label}.action must be one of: " + ", ".join(allowed_actions))
        if not acceptance_impact:
            errors.append(f"{hint_label} must declare acceptance_impact")
        if target and action and acceptance_impact:
            actions.append(
                {
                    "finding_index": index,
                    "hint_index": hint_index,
                    "target": target,
                    "action": action,
                    "acceptance_impact": acceptance_impact,
                }
            )


def _required_string_list(template: Mapping[str, Any], field: str, *, file_name: str) -> list[str]:
    values = _string_values(template.get(field))
    if not values:
        raise MalformedReviewPackError(f"{file_name}: {field} must be a non-empty list")
    return values


def _string_values(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    return []


def _require_non_empty_string_list(value: Any, errors: list[str], *, label: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        errors.append(f"{label} must be a non-empty list of strings")


def _source_path(template: Mapping[str, Any]) -> str:
    source = template.get("source")
    if isinstance(source, Mapping) and isinstance(source.get("path"), str):
        return source["path"]
    return str(template.get("template_id") or "<review pack>")


def _aliased_str(mapping: Mapping[str, Any], aliases: Iterable[str]) -> str:
    for alias in aliases:
        value = mapping.get(alias)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}
