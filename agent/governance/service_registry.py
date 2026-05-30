"""Deterministic governance service registry for contract-bound routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any


ServiceHandler = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


READ_SIDE_EFFECTS = {"none", "read", "gate"}
WRITE_SIDE_EFFECTS = {"write"}
ALLOWED_SERVICE_MODES = {"preview", "gate", "apply"}
ALLOWED_SIDE_EFFECTS = {*READ_SIDE_EFFECTS, *WRITE_SIDE_EFFECTS}
ROUTE_PROMPT_BUNDLE_SCHEMA_VERSION = "aming_route_prompt_alert_bundle.v1"
VISIBLE_INJECTION_MANIFEST_SCHEMA_VERSION = "visible_injection_manifest.v1"
ROUTE_PROMPT_CONTRACT_SCHEMA_VERSION = "route_prompt_contract.v1"

DEFAULT_ROUTE_ALERTS: tuple[dict[str, Any], ...] = (
    {
        "code": "observer_judger_must_not_implement",
        "severity": "block",
        "applies_to": ["observer", "judger"],
        "blocked_actions": [
            "apply_patch",
            "edit_file",
            "edit_files",
            "write_file",
            "implementation_exec",
            "mutate_files",
            "run_implementation_command",
        ],
    },
    {
        "code": "implementation_prompt_must_live_in_route",
        "severity": "block",
        "applies_to": ["observer", "judger", "implementation_worker", "mf_sub", "qa"],
        "blocked_actions": [
            "use_untracked_implementation_prompt",
            "dispatch_worker_without_route_prompt_contract",
        ],
    },
    {
        "code": "judger_reuses_route_context",
        "severity": "warning",
        "applies_to": ["judger"],
    },
    {
        "code": "identity_context_not_skill_text",
        "severity": "info",
        "applies_to": ["observer", "judger", "implementation_worker", "mf_sub", "qa"],
    },
    {
        "code": "external_injection_requires_visible_route_ref",
        "severity": "block",
        "applies_to": ["observer", "judger", "implementation_worker", "mf_sub", "qa"],
        "blocked_actions": [
            "use_untracked_external_prompt",
            "use_context_outside_visible_injection_manifest",
        ],
    },
    {
        "code": "strategic_task_requires_parallel_lanes",
        "severity": "block",
        "applies_to": ["observer", "judger"],
    },
)


@dataclass(frozen=True)
class ServiceDescriptor:
    """Static descriptor for a deterministic local governance service."""

    service_id: str
    mode: str
    side_effect: str
    supported_events: tuple[str, ...] = field(default_factory=tuple)
    required_permissions: tuple[str, ...] = field(default_factory=tuple)
    idempotency_fields: tuple[str, ...] = field(default_factory=tuple)
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    handler: ServiceHandler | None = None

    def __post_init__(self) -> None:
        if not self.service_id:
            raise ValueError("service_id is required")
        if self.mode not in ALLOWED_SERVICE_MODES:
            raise ValueError(f"{self.service_id}: unsupported service mode {self.mode!r}")
        if self.side_effect not in ALLOWED_SIDE_EFFECTS:
            raise ValueError(f"{self.service_id}: unsupported side_effect {self.side_effect!r}")
        if self.mode == "apply" or self.side_effect in WRITE_SIDE_EFFECTS:
            if not self.required_permissions:
                raise ValueError(f"{self.service_id}: apply/write service requires permissions")

    @property
    def side_effect_class(self) -> str:
        """Canonical route field alias for the descriptor side-effect class."""

        return self.side_effect


class ServiceRegistry:
    """In-memory registry for deterministic local governance service descriptors."""

    def __init__(self, descriptors: Mapping[str, ServiceDescriptor] | None = None) -> None:
        self._descriptors: dict[str, ServiceDescriptor] = dict(descriptors or {})

    def register(self, descriptor: ServiceDescriptor) -> None:
        self._descriptors[descriptor.service_id] = descriptor

    def get(self, service_id: str) -> ServiceDescriptor | None:
        return self._descriptors.get(service_id)

    def require(self, service_id: str) -> ServiceDescriptor:
        descriptor = self.get(service_id)
        if descriptor is None:
            raise KeyError(service_id)
        return descriptor

    def ids(self) -> set[str]:
        return set(self._descriptors)

    def as_dict(self) -> dict[str, ServiceDescriptor]:
        return dict(self._descriptors)


def deterministic_default_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a compact deterministic service summary without external calls."""

    service_id = str(route_context.get("service_id") or "")
    route_id = str(route_context.get("route_id") or "")
    event_kind = str(event.get("event_kind") or event.get("kind") or "")
    return {
        "ok": True,
        "service_id": service_id,
        "route_id": route_id,
        "event_kind": event_kind,
        "summary": f"{service_id} handled {event_kind} via {route_id}",
    }


def observer_reminder_echo_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Echo only the safe observer reminder fields for demo evidence."""

    service_id = str(route_context.get("service_id") or "")
    route_id = str(route_context.get("route_id") or "")
    event_kind = str(event.get("event_kind") or event.get("kind") or "")
    reminder = _reminder_payload(event)
    payload_included = reminder.get("payload_included")
    received_reminder = {
        "kind": _text(reminder.get("kind")),
        "project_id": _text(reminder.get("project_id") or event.get("project_id")),
        "message": _text(reminder.get("message")),
        "payload_included": payload_included if isinstance(payload_included, bool) else False,
        "next_action": _safe_next_action(
            reminder.get("next_action") or reminder.get("claim_instruction")
        ),
    }
    return {
        "ok": True,
        "service_id": service_id,
        "route_id": route_id,
        "event_kind": event_kind,
        "received_reminder": received_reminder,
        "received_reminder_echo": received_reminder,
        "payload_boundary": {
            "payload_included": received_reminder["payload_included"],
            "business_payload_excluded": True,
            "safe_fields": list(received_reminder.keys()),
        },
    }


def route_prompt_alert_bundle_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a compact visible route bundle without raw prompt material."""

    service_id = _text(route_context.get("service_id"))
    service_route_id = _text(route_context.get("route_id"))
    event_kind = _text(event.get("event_kind") or event.get("kind"))
    payload = _route_prompt_payload(event)
    alerts = _route_alerts(payload.get("route_alerts"))
    content = _route_content(payload)
    route = _route_identity(payload, event)
    prompt_contract = _route_prompt_contract(payload)
    visible_manifest = _visible_injection_manifest(
        payload.get("visible_injection_manifest")
    )
    prompt_contract_hash = _sha256_json(prompt_contract)
    bundle_without_hashes = {
        "schema_version": ROUTE_PROMPT_BUNDLE_SCHEMA_VERSION,
        "intent": _text(payload.get("intent") or payload.get("route_intent") or "implementation"),
        "content": content,
        "route": route,
        "alerts": alerts,
        "prompt_contract": prompt_contract,
        "visible_injection_manifest": visible_manifest,
        "prompt_contract_hash": prompt_contract_hash,
    }
    route_context_hash = _sha256_json(bundle_without_hashes)
    bundle = {
        **bundle_without_hashes,
        "route_context_hash": route_context_hash,
    }
    return {
        "ok": True,
        "service_id": service_id,
        "route_id": service_route_id,
        "event_kind": event_kind,
        "route_prompt_bundle": bundle,
        "bundle": bundle,
        "hashes": {
            "route_context_hash": route_context_hash,
            "prompt_contract_hash": prompt_contract_hash,
        },
        "payload_boundary": {
            "prompt_text_excluded": True,
            "context_text_excluded": True,
            "visible_manifest_only": True,
        },
    }


def route_action_precheck_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the local route action gate from a deterministic service route."""

    from agent.governance.mf_subagent_contract import (
        MfSubagentContractError,
        ROUTE_ACTION_GATE_SCHEMA_VERSION,
        validate_route_action_gate,
    )

    payload = _route_prompt_payload(event)
    try:
        evidence = validate_route_action_gate(payload)
        ok = True
        status = "allowed"
        reason = ""
    except MfSubagentContractError as exc:
        evidence = _route_action_blocked_evidence(
            payload,
            schema_version=ROUTE_ACTION_GATE_SCHEMA_VERSION,
            reason=_text(exc),
        )
        ok = False
        status = evidence["status"]
        reason = evidence["reason"]
    return {
        "ok": ok,
        "allowed": ok,
        "status": status,
        "reason": reason,
        "service_id": _text(route_context.get("service_id")),
        "route_id": _text(route_context.get("route_id")),
        "event_kind": _text(event.get("event_kind") or event.get("kind")),
        "route_action_gate": evidence,
        "payload_boundary": {
            "prompt_text_excluded": True,
            "context_text_excluded": True,
            "visible_manifest_only": True,
        },
    }


def _reminder_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    for key in ("hook_reminder", "received_reminder"):
        value = event.get(key)
        if isinstance(value, Mapping):
            return value
        value = payload_map.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _route_prompt_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    for key in ("route_context", "route_prompt_bundle", "prompt_route"):
        value = payload_map.get(key)
        if isinstance(value, Mapping):
            return {**dict(payload_map), **dict(value)}
    return payload_map if payload_map else event


def _route_action_blocked_evidence(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "allowed": False,
        "status": "route_action_policy_blocked",
        "reason": reason,
        "action": _text(
            payload.get("action")
            or payload.get("requested_action")
            or payload.get("tool_name")
        ),
        "caller_role": _text(
            payload.get("caller_role")
            or payload.get("role")
            or payload.get("actor_role")
        ),
        "route_context_hash": _payload_route_context_hash(payload),
        "prompt_contract_id": _payload_prompt_contract_id(payload),
        "prompt_contract_hash": _payload_prompt_contract_hash(payload),
    }


def _payload_route_context_hash(payload: Mapping[str, Any]) -> str:
    route_context = payload.get("route_context")
    route_map = route_context if isinstance(route_context, Mapping) else {}
    prompt_contract = payload.get("prompt_contract")
    prompt_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_prompt_contract = payload.get("route_prompt_contract")
    route_prompt_map = (
        route_prompt_contract if isinstance(route_prompt_contract, Mapping) else {}
    )
    return _text(
        payload.get("route_context_hash")
        or route_map.get("route_context_hash")
        or prompt_map.get("route_context_hash")
        or route_prompt_map.get("route_context_hash")
    )


def _payload_prompt_contract_id(payload: Mapping[str, Any]) -> str:
    route_context = payload.get("route_context")
    route_map = route_context if isinstance(route_context, Mapping) else {}
    prompt_contract = payload.get("prompt_contract")
    prompt_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_prompt_contract = payload.get("route_prompt_contract")
    route_prompt_map = (
        route_prompt_contract if isinstance(route_prompt_contract, Mapping) else {}
    )
    return _text(
        payload.get("prompt_contract_id")
        or route_map.get("prompt_contract_id")
        or prompt_map.get("prompt_contract_id")
        or prompt_map.get("id")
        or route_prompt_map.get("prompt_contract_id")
        or route_prompt_map.get("id")
    )


def _payload_prompt_contract_hash(payload: Mapping[str, Any]) -> str:
    route_context = payload.get("route_context")
    route_map = route_context if isinstance(route_context, Mapping) else {}
    prompt_contract = payload.get("prompt_contract")
    prompt_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_prompt_contract = payload.get("route_prompt_contract")
    route_prompt_map = (
        route_prompt_contract if isinstance(route_prompt_contract, Mapping) else {}
    )
    return _text(
        payload.get("prompt_contract_hash")
        or prompt_map.get("prompt_contract_hash")
        or route_map.get("prompt_contract_hash")
        or route_prompt_map.get("prompt_contract_hash")
    )


def _route_alerts(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) and value else list(DEFAULT_ROUTE_ALERTS)
    alerts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in source:
        if isinstance(item, str):
            alert = {"code": _text(item)}
        elif isinstance(item, Mapping):
            alert = {
                "code": _text(item.get("code")),
                "severity": _text(item.get("severity") or "info"),
                "applies_to": _list_of_text(item.get("applies_to")),
                "blocked_actions": _list_of_text(item.get("blocked_actions")),
            }
        else:
            continue
        code = alert.get("code", "")
        if not code or code in seen:
            continue
        seen.add(code)
        alerts.append({key: value for key, value in alert.items() if value not in ("", [])})
    return alerts


def _route_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    content = payload.get("content")
    content_map = content if isinstance(content, Mapping) else {}
    summary = _text(
        content_map.get("summary")
        or payload.get("content_summary")
        or payload.get("task_summary")
        or payload.get("summary")
    )
    kind = _text(content_map.get("kind") or payload.get("content_kind") or "task_summary")
    visible = {
        "kind": kind,
        "summary": summary,
        "prompt_text_included": False,
        "context_text_included": False,
    }
    visible["content_hash"] = _sha256_json({
        "kind": visible["kind"],
        "summary": visible["summary"],
    })
    return visible


def _route_identity(payload: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    route = payload.get("route")
    route_map = route if isinstance(route, Mapping) else {}
    return {
        "route_id": _text(route_map.get("route_id") or payload.get("route_id") or event.get("route_id")),
        "stage": _text(route_map.get("stage") or payload.get("stage") or event.get("stage")),
        "caller_role": _text(
            route_map.get("caller_role")
            or payload.get("caller_role")
            or payload.get("role")
            or event.get("caller_role")
        ),
        "route_intent": _text(
            route_map.get("route_intent")
            or payload.get("route_intent")
            or payload.get("intent")
            or "implementation"
        ),
    }


def _route_prompt_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    prompt_contract = payload.get("prompt_contract")
    contract_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    return {
        "schema_version": _text(
            contract_map.get("schema_version") or ROUTE_PROMPT_CONTRACT_SCHEMA_VERSION
        ),
        "prompt_contract_id": _text(
            contract_map.get("prompt_contract_id")
            or contract_map.get("id")
            or payload.get("prompt_contract_id")
        ),
        "prompt_kind": _text(
            contract_map.get("prompt_kind")
            or payload.get("prompt_kind")
            or "implementation"
        ),
        "target_files": _list_of_text(contract_map.get("target_files") or payload.get("target_files")),
        "acceptance_criteria": _list_of_text(
            contract_map.get("acceptance_criteria") or payload.get("acceptance_criteria")
        ),
        "evidence_required": _list_of_text(
            contract_map.get("evidence_required") or payload.get("evidence_required")
        ),
    }


def _visible_injection_manifest(value: Any) -> dict[str, Any]:
    manifest = value if isinstance(value, Mapping) else {}
    allowed = manifest.get("allowed_injections")
    entries = allowed if isinstance(allowed, list) else []
    compact_entries: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        compact = {
            "kind": _text(entry.get("kind")),
            "id": _text(entry.get("id")),
            "source_ref": _text(entry.get("source_ref") or entry.get("path") or entry.get("ref")),
            "sha256": _text(entry.get("sha256") or entry.get("hash") or entry.get("content_hash")),
            "status": _text(entry.get("status")),
        }
        compact_entries.append(
            {key: item for key, item in compact.items() if item}
        )
    return {
        "schema_version": _text(
            manifest.get("schema_version") or VISIBLE_INJECTION_MANIFEST_SCHEMA_VERSION
        ),
        "policy": _text(manifest.get("policy") or "route_owned_visible_refs_only"),
        "allowed_injections": compact_entries,
    }


def _list_of_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_next_action(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {
            "tool": _text(value.get("tool") or "observer_command_next"),
            "description": _text(
                value.get("description") or "claim the next pending observer command"
            ),
        }
    description = _text(value) or "claim the next pending observer command"
    return {
        "tool": "observer_command_next",
        "description": description,
    }


def default_service_descriptors() -> dict[str, ServiceDescriptor]:
    """Return the built-in deterministic governance service descriptors."""

    common_idempotency = ("event_id", "event_kind", "stage", "task_id", "backlog_id")
    return {
        "test_governance.preview": ServiceDescriptor(
            service_id="test_governance.preview",
            mode="preview",
            side_effect="read",
            supported_events=("task.completed", "ai.structured_output.validated"),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "cleanup.preview": ServiceDescriptor(
            service_id="cleanup.preview",
            mode="preview",
            side_effect="read",
            supported_events=("cleanup.requested",),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "cleanup.apply": ServiceDescriptor(
            service_id="cleanup.apply",
            mode="apply",
            side_effect="write",
            supported_events=("cleanup.requested",),
            required_permissions=("cleanup.apply",),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "review.recommendations": ServiceDescriptor(
            service_id="review.recommendations",
            mode="preview",
            side_effect="read",
            supported_events=("task.completed", "review.requested"),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "precheck.run": ServiceDescriptor(
            service_id="precheck.run",
            mode="gate",
            side_effect="gate",
            supported_events=("precheck.requested", "stage.completed"),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "gate.close": ServiceDescriptor(
            service_id="gate.close",
            mode="gate",
            side_effect="gate",
            supported_events=("backlog.close.requested",),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "observer.reminder_echo": ServiceDescriptor(
            service_id="observer.reminder_echo",
            mode="preview",
            side_effect="read",
            supported_events=("observer.command.notified",),
            idempotency_fields=(
                "event_id",
                "event_kind",
                "project_id",
                "route_id",
                "service_id",
            ),
            handler=observer_reminder_echo_handler,
        ),
        "route.prompt_alert_bundle": ServiceDescriptor(
            service_id="route.prompt_alert_bundle",
            mode="preview",
            side_effect="read",
            supported_events=("route.prompt_context.requested",),
            idempotency_fields=(
                "event_id",
                "event_kind",
                "stage",
                "payload.route_id",
                "payload.prompt_contract_id",
            ),
            handler=route_prompt_alert_bundle_handler,
        ),
        "route.action_precheck": ServiceDescriptor(
            service_id="route.action_precheck",
            mode="gate",
            side_effect="gate",
            supported_events=("route.action.requested",),
            idempotency_fields=(
                "event_id",
                "event_kind",
                "stage",
                "payload.route_context_hash",
                "payload.prompt_contract_id",
                "payload.prompt_contract_hash",
                "payload.action",
                "payload.caller_role",
            ),
            handler=route_action_precheck_handler,
        ),
    }


def default_service_registry() -> ServiceRegistry:
    """Build a registry with built-in deterministic governance services."""

    return ServiceRegistry(default_service_descriptors())
