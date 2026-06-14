"""Source-controlled governance contract template registry."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from pathlib import Path
from typing import Any

from agent.governance.service_registry import (
    ALLOWED_SERVICE_MODES,
    ALLOWED_SIDE_EFFECTS,
    WRITE_SIDE_EFFECTS,
    default_service_registry,
)


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "contract_templates"
FORBIDDEN_ROUTE_KEYS = {"ai_provider", "model", "prompt", "llm", "ai_call"}
NON_TEMPLATE_CONTRACT_FILES = {"meta_contract.v1.json"}


class ContractTemplateError(ValueError):
    """Base error for contract template registry failures."""


class UnknownContractTemplateError(ContractTemplateError):
    """Raised when no source-controlled template matches a requested key."""


class MalformedContractTemplateError(ContractTemplateError):
    """Raised when a template file is not usable by the registry."""


def _template_paths(template_dir: str | Path = DEFAULT_TEMPLATE_DIR) -> list[Path]:
    root = Path(template_dir)
    if not root.exists():
        return []
    return sorted(
        (
            path
            for path in root.glob("*.json")
            if not path.name.endswith(".schema.json")
            and path.name not in NON_TEMPLATE_CONTRACT_FILES
        ),
        key=lambda item: item.name,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedContractTemplateError(f"{path.name}: invalid json: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MalformedContractTemplateError(f"{path.name}: template root must be an object")
    return payload


def _string_list(value: Any, *, file_name: str, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise MalformedContractTemplateError(f"{file_name}: {field} must be a list of strings")
    return list(value)


def _template_version(template_id: str, payload: Mapping[str, Any]) -> str:
    version = payload.get("version")
    if isinstance(version, str) and version:
        return version
    if "." in template_id:
        return template_id.rsplit(".", 1)[-1]
    return ""


def _validate_template(payload: Mapping[str, Any], *, file_name: str) -> dict[str, Any]:
    template_id = payload.get("template_id")
    schema_version = payload.get("schema_version")
    if not isinstance(template_id, str) or not template_id:
        raise MalformedContractTemplateError(f"{file_name}: missing template_id")
    if not isinstance(schema_version, str) or not schema_version:
        raise MalformedContractTemplateError(f"{file_name}: missing schema_version")

    task_types = _string_list(payload.get("task_types"), file_name=file_name, field="task_types")
    stages = _string_list(payload.get("stages"), file_name=file_name, field="stages")
    version = _template_version(template_id, payload)
    if not version:
        raise MalformedContractTemplateError(f"{file_name}: missing version")

    normalized = dict(payload)
    normalized["version"] = version
    normalized["task_types"] = task_types
    normalized["stages"] = stages
    normalized.setdefault("source", {"type": "source_controlled", "path": file_name})
    event_routes, service_routes = _validate_routes(payload, file_name=file_name)
    if "event_routes" in payload:
        normalized["event_routes"] = event_routes
    if "service_routes" in payload:
        normalized["service_routes"] = service_routes
    return normalized


def _validate_routes(
    payload: Mapping[str, Any],
    *,
    file_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    event_routes = _route_list(
        payload.get("event_routes"),
        file_name=file_name,
        field="event_routes",
        allow_mapping=True,
    )
    service_routes = _route_list(
        payload.get("service_routes"),
        file_name=file_name,
        field="service_routes",
        allow_mapping=True,
    )
    if not event_routes and not service_routes:
        return event_routes, service_routes

    _reject_ai_route_fields(event_routes, file_name=file_name, field="event_routes")
    _reject_ai_route_fields(service_routes, file_name=file_name, field="service_routes")

    service_route_ids = _unique_route_ids(
        service_routes,
        file_name=file_name,
        field="service_routes",
        required=True,
    )
    _unique_route_ids(event_routes, file_name=file_name, field="event_routes", required=True)

    known_services = default_service_registry().ids()
    registry = default_service_registry()
    services_declared_by_route_id: dict[str, str] = {}
    for route in service_routes:
        route_id = str(route["route_id"])
        service_id = _required_str(route, "service_id", file_name=file_name, field="service_routes")
        services_declared_by_route_id[route_id] = service_id
        if service_id not in known_services:
            raise MalformedContractTemplateError(
                f"{file_name}: service_routes {route_id} unknown service_id {service_id!r}"
            )
        _validate_service_route(route, file_name=file_name, route_id=route_id)

    for route in event_routes:
        route_id = str(route["route_id"])
        event_kind = _required_str(route, "event_kind", file_name=file_name, field="event_routes")
        if not event_kind:
            raise MalformedContractTemplateError(f"{file_name}: event_routes {route_id} missing event_kind")
        _validate_event_route_stage_fields(route, file_name=file_name, route_id=route_id)
        service_route_id = route.get("service_route_id")
        service_id = route.get("service_id")
        if service_route_id is not None and not isinstance(service_route_id, str):
            raise MalformedContractTemplateError(
                f"{file_name}: event_routes {route_id} service_route_id must be a string"
            )
        if isinstance(service_route_id, str) and service_route_id:
            if service_route_id not in service_route_ids:
                raise MalformedContractTemplateError(
                    f"{file_name}: event_routes {route_id} unknown service_route_id {service_route_id!r}"
                )
            service_id = services_declared_by_route_id[service_route_id]
            _validate_supported_event(file_name, route_id, event_kind, service_id, registry)
            continue
        if not isinstance(service_id, str) or not service_id:
            raise MalformedContractTemplateError(
                f"{file_name}: event_routes {route_id} must declare service_id or service_route_id"
            )
        if service_id not in known_services and service_id not in services_declared_by_route_id.values():
            raise MalformedContractTemplateError(
                f"{file_name}: event_routes {route_id} unknown service_id {service_id!r}"
            )
        _validate_supported_event(file_name, route_id, event_kind, service_id, registry)

    return event_routes, service_routes


def _validate_event_route_stage_fields(
    route: Mapping[str, Any],
    *,
    file_name: str,
    route_id: str,
) -> None:
    stage = route.get("stage")
    if stage is not None and not isinstance(stage, str):
        raise MalformedContractTemplateError(
            f"{file_name}: event_routes {route_id} stage must be a string"
        )
    stages = route.get("stages")
    if stages is not None:
        if not isinstance(stages, list) or not stages or not all(
            isinstance(item, str) and item for item in stages
        ):
            raise MalformedContractTemplateError(
                f"{file_name}: event_routes {route_id} stages must be a non-empty list of strings"
            )


def _validate_supported_event(
    file_name: str,
    route_id: str,
    event_kind: str,
    service_id: str,
    registry: Any,
) -> None:
    descriptor = registry.get(service_id)
    if descriptor and descriptor.supported_events and event_kind not in descriptor.supported_events:
        raise MalformedContractTemplateError(
            f"{file_name}: event_routes {route_id} service {service_id!r} "
            f"does not support event_kind {event_kind!r}"
        )


def _validate_service_route(route: Mapping[str, Any], *, file_name: str, route_id: str) -> None:
    mode = _required_str(route, "mode", file_name=file_name, field=f"service_routes {route_id}")
    side_effect_class = _route_side_effect_class(
        route,
        file_name=file_name,
        route_id=route_id,
    )
    if mode not in ALLOWED_SERVICE_MODES:
        raise MalformedContractTemplateError(
            f"{file_name}: service_routes {route_id} unsupported mode {mode!r}"
        )
    if side_effect_class not in ALLOWED_SIDE_EFFECTS:
        raise MalformedContractTemplateError(
            f"{file_name}: service_routes {route_id} unsupported side_effect_class {side_effect_class!r}"
        )
    _validate_idempotency_policy(
        route.get("idempotency_key_policy"),
        file_name=file_name,
        field=f"service_routes {route_id}.idempotency_key_policy",
    )
    if mode == "apply" or side_effect_class in WRITE_SIDE_EFFECTS:
        permissions = route.get("required_permissions")
        if not isinstance(permissions, list) or not all(
            isinstance(permission, str) and permission for permission in permissions
        ):
            raise MalformedContractTemplateError(
                f"{file_name}: service_routes {route_id} apply/write requires "
                "required_permissions for side_effect_class"
            )


def _route_side_effect_class(route: Mapping[str, Any], *, file_name: str, route_id: str) -> str:
    value = route.get("side_effect_class")
    if value is None:
        value = route.get("side_effect")
    if not isinstance(value, str) or not value:
        raise MalformedContractTemplateError(
            f"{file_name}: service_routes {route_id} missing side_effect_class"
        )
    return value


def _route_list(
    value: Any,
    *,
    file_name: str,
    field: str,
    allow_mapping: bool = False,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        routes: list[dict[str, Any]] = []
        for index, route in enumerate(value):
            if not isinstance(route, Mapping):
                raise MalformedContractTemplateError(
                    f"{file_name}: {field}[{index}] must be an object"
                )
            routes.append(dict(route))
        return routes
    if allow_mapping and isinstance(value, Mapping):
        routes = []
        for route_id, route in value.items():
            if not isinstance(route_id, str) or not route_id:
                raise MalformedContractTemplateError(f"{file_name}: {field} keys must be strings")
            if not isinstance(route, Mapping):
                raise MalformedContractTemplateError(f"{file_name}: {field}.{route_id} must be an object")
            normalized = dict(route)
            normalized.setdefault("route_id", route_id)
            routes.append(normalized)
        return routes
    expected = "a list or object" if allow_mapping else "a list"
    raise MalformedContractTemplateError(f"{file_name}: {field} must be {expected}")


def _unique_route_ids(
    routes: list[Mapping[str, Any]],
    *,
    file_name: str,
    field: str,
    required: bool,
) -> set[str]:
    seen: set[str] = set()
    for index, route in enumerate(routes):
        route_id = route.get("route_id")
        if not isinstance(route_id, str) or not route_id:
            if required:
                raise MalformedContractTemplateError(
                    f"{file_name}: {field}[{index}] missing route_id"
                )
            continue
        if route_id in seen:
            raise MalformedContractTemplateError(
                f"{file_name}: {field} duplicate route_id {route_id!r}"
            )
        seen.add(route_id)
    return seen


def _required_str(route: Mapping[str, Any], key: str, *, file_name: str, field: str) -> str:
    value = route.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedContractTemplateError(f"{file_name}: {field} missing {key}")
    return value


def _validate_idempotency_policy(value: Any, *, file_name: str, field: str) -> None:
    if isinstance(value, Mapping):
        fields = value.get("fields")
        if not isinstance(fields, list) or not all(isinstance(item, str) and item for item in fields):
            raise MalformedContractTemplateError(f"{file_name}: {field}.fields must be a list of strings")
        return
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return
    raise MalformedContractTemplateError(f"{file_name}: {field} must declare fields")


def _reject_ai_route_fields(routes: list[Mapping[str, Any]], *, file_name: str, field: str) -> None:
    for route in routes:
        bad_key = _first_forbidden_key(route)
        if bad_key:
            route_id = str(route.get("route_id") or "<missing>")
            raise MalformedContractTemplateError(
                f"{file_name}: {field} {route_id} contains forbidden AI field {bad_key!r}"
            )


def _first_forbidden_key(value: Any) -> str:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in FORBIDDEN_ROUTE_KEYS:
                return key_text
            found = _first_forbidden_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_forbidden_key(child)
            if found:
                return found
    return ""


def load_contract_templates(template_dir: str | Path = DEFAULT_TEMPLATE_DIR) -> list[dict[str, Any]]:
    """Load and validate all source-controlled contract templates."""

    templates: list[dict[str, Any]] = []
    for path in _template_paths(template_dir):
        templates.append(_validate_template(_load_json(path), file_name=path.name))
    return sorted(templates, key=lambda item: str(item["template_id"]))


def list_contract_templates(
    *,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
    task_type: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    """List templates, optionally filtered by task type and stage."""

    templates = load_contract_templates(template_dir)
    return [
        template
        for template in templates
        if _matches(template, task_type=task_type, stage=stage)
    ]


def get_contract_template(
    template_id: str,
    *,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Return a template by exact versioned template id."""

    for template in load_contract_templates(template_dir):
        if template["template_id"] == template_id:
            return template
    raise UnknownContractTemplateError(f"unknown contract template: {template_id}")


def resolve_contract_template(
    *,
    template_id: str | None = None,
    task_type: str | None = None,
    stage: str | None = None,
    version: str | None = None,
    template_dir: str | Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    """Resolve a template by exact id, id plus version, or task_type/stage."""

    templates = list_contract_templates(template_dir=template_dir, task_type=task_type, stage=stage)
    if template_id:
        templates = [
            template
            for template in templates
            if template["template_id"] == template_id
            or (
                version
                and str(template["template_id"]) == f"{template_id}.{version}"
            )
        ]
    if version:
        templates = [template for template in templates if template.get("version") == version]
    if not templates:
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
        raise UnknownContractTemplateError(f"unknown contract template resolution: {key or 'empty query'}")
    if len(templates) > 1:
        raise ContractTemplateError(
            "ambiguous contract template resolution: "
            + ", ".join(str(template["template_id"]) for template in templates)
        )
    return templates[0]


def _matches(template: Mapping[str, Any], *, task_type: str | None, stage: str | None) -> bool:
    task_types = _as_set(template.get("task_types"))
    stages = _as_set(template.get("stages"))
    if task_type and task_type not in task_types:
        return False
    if stage and stage not in stages:
        return False
    return True


def _as_set(values: Any) -> set[str]:
    if isinstance(values, Iterable) and not isinstance(values, (str, bytes, dict)):
        return {str(value) for value in values}
    return set()
