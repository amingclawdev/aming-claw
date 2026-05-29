"""Contract-bound event router for deterministic governance services."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from agent.governance.service_registry import (
    ServiceDescriptor,
    ServiceRegistry,
    WRITE_SIDE_EFFECTS,
    default_service_registry,
)


def route_event(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    registry: ServiceRegistry | None = None,
    *,
    call_handlers: bool = True,
) -> dict[str, Any]:
    """Resolve one governance event against a contract route table."""

    router = ServiceRouter(registry or default_service_registry())
    return router.route(event, contract, call_handlers=call_handlers)


class ServiceRouter:
    """Small deterministic router over contract event/service route declarations."""

    def __init__(self, registry: ServiceRegistry | None = None) -> None:
        self.registry = registry or default_service_registry()

    def route(
        self,
        event: Mapping[str, Any],
        contract: Mapping[str, Any],
        *,
        call_handlers: bool = True,
    ) -> dict[str, Any]:
        event_kind = _event_kind(event)
        stage = _string(event.get("stage"))
        event_routes = _event_routes(contract)
        service_routes = _service_routes(contract)
        matching_routes = [
            route for route in event_routes if _route_matches(route, event_kind=event_kind, stage=stage)
        ]
        if not matching_routes:
            return {
                "status": "no_op",
                "decision": "no_op",
                "event_kind": event_kind,
                "route_count": 0,
                "routes": [],
            }

        results = [
            self._run_route(
                event,
                contract,
                event_route,
                service_routes,
                call_handlers=call_handlers,
            )
            for event_route in matching_routes
        ]
        blocked = [result for result in results if result["decision"] == "block"]
        allowed = [result for result in results if result["decision"] == "allow"]
        return {
            "status": "blocked" if blocked else "routed",
            "decision": "block" if blocked else "allow",
            "event_kind": event_kind,
            "route_count": len(results),
            "routes": results,
            "allowed_count": len(allowed),
            "blocked_count": len(blocked),
        }

    def _run_route(
        self,
        event: Mapping[str, Any],
        contract: Mapping[str, Any],
        event_route: Mapping[str, Any],
        service_routes: Mapping[str, Mapping[str, Any]],
        *,
        call_handlers: bool,
    ) -> dict[str, Any]:
        route_id = _route_id(event_route)
        service_route = _resolve_service_route(event_route, service_routes)
        service_id = _string(
            service_route.get("service_id")
            or event_route.get("service_id")
            or event_route.get("service")
        )
        descriptor = self.registry.get(service_id)
        if descriptor is None:
            return {
                "route_id": route_id,
                "service_id": service_id,
                "decision": "block",
                "status": "unknown_service",
                "reason": f"unknown service: {service_id}",
                "idempotency_key": "",
            }
        event_kind = _event_kind(event)
        if descriptor.supported_events and event_kind not in descriptor.supported_events:
            return {
                "route_id": route_id,
                "service_id": service_id,
                "decision": "block",
                "status": "unsupported_event",
                "reason": f"{service_id} does not support {event_kind}",
                "idempotency_key": "",
            }

        merged = _merge_descriptor_route(descriptor, service_route, event_route)
        idempotency_key = _idempotency_key(event, contract, merged, route_id, service_id)
        permission_check = _permission_check(event, contract, merged)
        if permission_check:
            return {
                "route_id": route_id,
                "service_id": service_id,
                "mode": merged["mode"],
                "side_effect_class": merged["side_effect_class"],
                "side_effect": merged["side_effect_class"],
                "decision": "block",
                "status": "permission_blocked",
                "reason": permission_check,
                "idempotency_key": idempotency_key,
            }

        result_summary: dict[str, Any] | None = None
        if call_handlers:
            handler = descriptor.handler
            if handler is not None:
                result_summary = handler(
                    event,
                    {
                        "contract_id": _contract_id(contract),
                        "route_id": route_id,
                        "service_id": service_id,
                        "mode": merged["mode"],
                        "side_effect_class": merged["side_effect_class"],
                        "side_effect": merged["side_effect_class"],
                        "idempotency_key": idempotency_key,
                    },
                )
        if result_summary is None:
            result_summary = {
                "ok": True,
                "summary": f"{service_id} allowed for {_event_kind(event)}",
            }
        return {
            "route_id": route_id,
            "service_id": service_id,
            "mode": merged["mode"],
            "side_effect_class": merged["side_effect_class"],
            "side_effect": merged["side_effect_class"],
            "decision": "allow",
            "status": "allowed",
            "idempotency_key": idempotency_key,
            "result": result_summary,
        }


def _event_routes(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    routes = contract.get("event_routes") or []
    if isinstance(routes, list):
        return [route for route in routes if isinstance(route, Mapping)]
    if isinstance(routes, Mapping):
        return [
            {**dict(route), "route_id": str(route.get("route_id") or route_id)}
            for route_id, route in routes.items()
            if isinstance(route, Mapping)
        ]
    return []


def _service_routes(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    routes = contract.get("service_routes") or {}
    if isinstance(routes, Mapping):
        return {
            str(route_id): {**dict(route), "route_id": str(route.get("route_id") or route_id)}
            for route_id, route in routes.items()
            if isinstance(route, Mapping)
        }
    if isinstance(routes, list):
        out: dict[str, Mapping[str, Any]] = {}
        for route in routes:
            if isinstance(route, Mapping):
                route_id = _route_id(route)
                out[route_id] = dict(route)
        return out
    return {}


def _route_matches(route: Mapping[str, Any], *, event_kind: str, stage: str) -> bool:
    if route.get("enabled") is False:
        return False
    route_event_kind = _string(route.get("event_kind") or route.get("kind"))
    if route_event_kind != event_kind:
        return False
    route_stage = _string(route.get("stage"))
    route_stages = _list_of_strings(route.get("stages"))
    if not route_stage and not route_stages:
        return True
    if not stage:
        return False
    return route_stage == stage or stage in route_stages


def _resolve_service_route(
    event_route: Mapping[str, Any],
    service_routes: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    service_route_id = _string(event_route.get("service_route_id"))
    if service_route_id:
        return service_routes.get(service_route_id, {})
    route_id = _route_id(event_route)
    return service_routes.get(route_id, {})


def _merge_descriptor_route(
    descriptor: ServiceDescriptor,
    service_route: Mapping[str, Any],
    event_route: Mapping[str, Any],
) -> dict[str, Any]:
    idempotency_policy = (
        service_route.get("idempotency_key_policy")
        or event_route.get("idempotency_key_policy")
        or {"fields": list(descriptor.idempotency_fields)}
    )
    return {
        "mode": _string(service_route.get("mode") or event_route.get("mode") or descriptor.mode),
        "side_effect_class": _string(
            service_route.get("side_effect_class")
            or event_route.get("side_effect_class")
            or service_route.get("side_effect")
            or event_route.get("side_effect")
            or descriptor.side_effect
        ),
        "required_permissions": _list_of_strings(
            service_route.get("required_permissions")
            or event_route.get("required_permissions")
            or list(descriptor.required_permissions)
        ),
        "idempotency_key_policy": idempotency_policy,
    }


def _permission_check(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    route: Mapping[str, Any],
) -> str:
    mode = _string(route.get("mode"))
    side_effect_class = _string(route.get("side_effect_class") or route.get("side_effect"))
    required = _list_of_strings(route.get("required_permissions"))
    if mode != "apply" and side_effect_class not in WRITE_SIDE_EFFECTS:
        return ""
    if not required:
        return "apply/write route requires explicit permissions"
    granted = {
        *_list_of_strings(event.get("permissions")),
        *_list_of_strings(event.get("granted_permissions")),
        *_list_of_strings(_mapping(event.get("payload")).get("permissions")),
        *_list_of_strings(contract.get("permissions")),
        *_list_of_strings(contract.get("granted_permissions")),
    }
    missing = [permission for permission in required if permission not in granted]
    if missing:
        return "missing permissions: " + ", ".join(missing)
    return ""


def _idempotency_key(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    route: Mapping[str, Any],
    route_id: str,
    service_id: str,
) -> str:
    policy = route.get("idempotency_key_policy")
    if isinstance(policy, Mapping):
        fields = _list_of_strings(policy.get("fields"))
    elif isinstance(policy, list):
        fields = _list_of_strings(policy)
    else:
        fields = []
    if not fields:
        fields = ["event_id", "event_kind", "stage", "task_id", "backlog_id"]
    values = {
        field: _value_for_field(event, contract, field, route_id=route_id, service_id=service_id)
        for field in fields
    }
    digest = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"service-route:{service_id}:{route_id}:{digest}"


def _value_for_field(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    field: str,
    *,
    route_id: str,
    service_id: str,
) -> Any:
    if field == "route_id":
        return route_id
    if field == "service_id":
        return service_id
    if field in {"event_kind", "kind"}:
        return _event_kind(event)
    if field == "contract_id":
        return _contract_id(contract)
    if "." in field:
        current: Any = {"event": event, "contract": contract, "payload": _mapping(event.get("payload"))}
        for part in field.split("."):
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                return ""
        return current if current is not None else ""
    if field == "event_id":
        return event.get("event_id") or event.get("source_event_id") or event.get("id") or ""
    if field in event:
        return event.get(field)
    payload = _mapping(event.get("payload"))
    if field in payload:
        return payload.get(field)
    if field in contract:
        return contract.get(field)
    return ""


def _event_kind(event: Mapping[str, Any]) -> str:
    return _string(event.get("event_kind") or event.get("kind"))


def _contract_id(contract: Mapping[str, Any]) -> str:
    return _string(
        contract.get("contract_instance_id")
        or contract.get("contract_id")
        or contract.get("template_id")
    )


def _route_id(route: Mapping[str, Any]) -> str:
    return _string(route.get("route_id") or route.get("id"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""
