"""Governed, public-safe Desktop host protocol for Codex-built-in agents.

This module deliberately does not control the Desktop application.  It models
the host side of an immutable execution ticket: registration, heartbeat,
acknowledgement, one worker join, and run-scoped cleanup.  Raw worker auth is
delivered by the Desktop host and is never accepted by this public contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


DESKTOP_HOST_REGISTRATION_SCHEMA_VERSION = (
    "cli_agent_service.desktop_host_registration.v1"
)
DESKTOP_HOST_HEARTBEAT_SCHEMA_VERSION = (
    "cli_agent_service.desktop_host_heartbeat.v1"
)
DESKTOP_EXECUTION_TICKET_ACK_SCHEMA_VERSION = (
    "cli_agent_service.desktop_execution_ticket_ack.v1"
)
DESKTOP_EXECUTION_TICKET_ADMISSION_SCHEMA_VERSION = (
    "cli_agent_service.desktop_execution_ticket_admission.v1"
)
DESKTOP_RUNTIME_JOIN_SCHEMA_VERSION = "cli_agent_service.desktop_runtime_join.v1"
CODEX_DESKTOP_HANDOFF_SCHEMA_VERSION = (
    "cli_agent_service.codex_desktop_handoff.v1"
)
DESKTOP_RUN_CLEANUP_SCHEMA_VERSION = "cli_agent_service.desktop_run_cleanup.v1"

_HOST_KINDS = frozenset({"codex_desktop", "claude_desktop"})
_AUTOMATION_MODES = frozenset({"service_callable", "user_triggered"})
_SAFE_TEXT = re.compile(r"[A-Za-z0-9/][A-Za-z0-9:._/@+ -]{0,511}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_RAW_FIELD_NAMES = frozenset(
    {
        "session_token",
        "fence_token",
        "route_token",
        "worker_session_token",
        "api_key",
        "password",
        "credential",
        "secret",
        "prompt",
        "prompt_body",
        "launch_text",
        "private_context",
        "host_envelope",
        "env",
        "environment",
    }
)
_COPY_SAFE_WORKER_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "status",
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "backlog_id",
        "worker_role",
        "worker_id",
        "worker_slot_id",
        "agent_id",
        "actual_host_worker_id",
        "worker_session_id",
        "worker_session_token_ref",
        "session_token_ref",
        "target_project_root",
        "worktree_path",
        "branch_ref",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "observer_command_id",
        "branch",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
        "initial_join",
        "rejoin",
        "worker_instructions",
        "security_boundary",
        "required_fields",
        "missing_fields",
        "session_token_ref_source",
        "raw_tokens_in_prompt_allowed",
        "raw_host_envelope_persisted",
        "raw_session_token_persisted",
        "raw_fence_token_persisted",
        "copy_safe",
        "claim_mode",
        "route_identity",
        "session_token_ref_expected_after_initial_join",
        "host_env_injection_required",
        "worker_claims_host_envelope",
        "host_envelope_delivery",
        "host_envelope_expected_fields",
        "session_token_ref_alone_authorizes_writes",
        "initial_join",
        "rejoin",
    }
)
_DISPATCH_FIELDS = (
    "project_id",
    "backlog_id",
    "task_id",
    "worker_id",
    "worker_slot_id",
    "parent_task_id",
    "runtime_context_id",
    "worker_role",
    "worktree_path",
    "target_project_root",
    "branch_ref",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "owned_files",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
    "observer_command_id",
)
_TICKET_AUTHORITY_HASH_FIELDS = (
    "execution_state_hash",
    "runtime_guide_hash",
    "next_legal_action_hash",
    "dispatch_identity_hash",
    "profile_requirements_hash",
    "retry_policy_hash",
)


class DesktopHostAdapterError(ValueError):
    """A Desktop host request failed without echoing supplied private data."""


def _timestamp(value: str = "") -> tuple[datetime, str]:
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DesktopHostAdapterError("Desktop host timestamp is invalid") from exc
        parsed = (
            parsed.astimezone(timezone.utc)
            if parsed.tzinfo
            else parsed.replace(tzinfo=timezone.utc)
        )
    else:
        parsed = datetime.now(timezone.utc)
    return parsed, parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_text(value: Any, field_name: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise DesktopHostAdapterError(
                "Desktop host {} is required".format(field_name)
            )
        return ""
    if not _SAFE_TEXT.fullmatch(text):
        raise DesktopHostAdapterError(
            "Desktop host {} is not a copy-safe identifier".format(field_name)
        )
    return text


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stable_ref(prefix: str, value: Any) -> str:
    return "{}-{}".format(prefix, _stable_hash(value).removeprefix("sha256:")[:24])


def _contains_raw_field(value: Any, *, allowed_fields: frozenset[str] | None = None) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key or "").strip().lower()
            if allowed_fields is not None and name not in allowed_fields:
                return True
            if name in _RAW_FIELD_NAMES:
                return True
            if name.endswith("_token") and not name.endswith("_token_ref"):
                return True
            if _contains_raw_field(child):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_contains_raw_field(child) for child in value)
    return False


def _copy_safe_worker_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise DesktopHostAdapterError("copy-safe worker envelope is required")
    if _contains_raw_field(value, allowed_fields=_COPY_SAFE_WORKER_FIELDS):
        raise DesktopHostAdapterError(
            "copy-safe worker envelope contains a private or unsupported field"
        )
    copied = deepcopy(dict(value))
    for key in (
        "runtime_context_id",
        "task_id",
        "parent_task_id",
        "session_token_ref",
        "target_project_root",
    ):
        _safe_text(copied.get(key), "worker envelope {}".format(key))
    copied["raw_worker_auth_exposed"] = False
    copied["copy_safe"] = True
    return copied


def _public_dispatch_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesktopHostAdapterError("execution ticket dispatch identity is missing")
    if _contains_raw_field(value):
        raise DesktopHostAdapterError(
            "execution ticket dispatch identity contains private material"
        )
    identity: dict[str, Any] = {}
    for field_name in _DISPATCH_FIELDS:
        field_value = value.get(field_name)
        if field_name == "owned_files":
            if isinstance(field_value, (list, tuple)):
                identity[field_name] = [
                    _safe_text(item, "owned file") for item in field_value
                ]
            continue
        if field_value not in (None, ""):
            identity[field_name] = _safe_text(
                field_value,
                "dispatch {}".format(field_name),
            )
    for required in (
        "project_id",
        "runtime_context_id",
        "task_id",
        "worker_id",
        "worker_slot_id",
        "observer_command_id",
    ):
        if not identity.get(required):
            raise DesktopHostAdapterError(
                "execution ticket dispatch {} is required".format(required)
            )
    if not identity.get("target_project_root"):
        worktree = str(identity.get("worktree_path") or "").strip()
        if not worktree:
            raise DesktopHostAdapterError(
                "execution ticket assigned worktree is required"
            )
        identity["target_project_root"] = worktree
    return identity


def _validated_issued_ticket(
    execution_ticket: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(execution_ticket, Mapping) or _contains_raw_field(
        execution_ticket
    ):
        raise DesktopHostAdapterError(
            "execution ticket is not a public-safe issued ticket"
        )
    ticket = deepcopy(dict(execution_ticket))
    if (
        ticket.get("status") != "issued"
        or ticket.get("issue_allowed") is not True
        or ticket.get("immutable") is not True
        or ticket.get("source_of_authority") != "ContractRuntime"
        or ticket.get("authority_decision_source")
        != "contract_runtime_completed_dispatch_line"
    ):
        raise DesktopHostAdapterError(
            "execution ticket lacks canonical ContractRuntime authority"
        )
    for field_name in ("contract_execution_id", "contract_revision_id"):
        _safe_text(ticket.get(field_name), field_name)
    try:
        execution_state_revision = int(ticket.get("execution_state_revision") or 0)
    except (TypeError, ValueError) as exc:
        raise DesktopHostAdapterError(
            "execution ticket state revision is invalid"
        ) from exc
    if execution_state_revision <= 0:
        raise DesktopHostAdapterError(
            "execution ticket state revision is required"
        )
    for field_name in _TICKET_AUTHORITY_HASH_FIELDS:
        field_value = _safe_text(ticket.get(field_name), field_name)
        if not _HASH.fullmatch(field_value):
            raise DesktopHostAdapterError(
                "execution ticket {} is invalid".format(field_name)
            )
    raw_dispatch = ticket.get("dispatch_identity")
    if not isinstance(raw_dispatch, Mapping) or _contains_raw_field(raw_dispatch):
        raise DesktopHostAdapterError(
            "execution ticket dispatch identity is invalid"
        )
    if ticket["dispatch_identity_hash"] != _stable_hash(dict(raw_dispatch)):
        raise DesktopHostAdapterError(
            "execution ticket dispatch identity hash is invalid"
        )
    for value_field, hash_field in (
        ("next_legal_action", "next_legal_action_hash"),
        ("profile_requirements", "profile_requirements_hash"),
        ("retry_policy", "retry_policy_hash"),
    ):
        value = ticket.get(value_field)
        if not isinstance(value, Mapping) or ticket[hash_field] != _stable_hash(
            dict(value)
        ):
            raise DesktopHostAdapterError(
                "execution ticket {} is invalid".format(hash_field)
            )
    dispatch = _public_dispatch_identity(raw_dispatch)
    ticket_id = _safe_text(ticket.get("ticket_id"), "ticket_id")
    ticket_hash = _safe_text(ticket.get("ticket_hash"), "ticket_hash")
    if not ticket_id.startswith("caet-") or not _HASH.fullmatch(ticket_hash):
        raise DesktopHostAdapterError("execution ticket immutable identity is invalid")
    if ticket_hash != _stable_hash(
        {key: value for key, value in ticket.items() if key != "ticket_hash"}
    ):
        raise DesktopHostAdapterError("execution ticket hash is invalid")
    return ticket, dispatch


class CodexDesktopAdapter:
    """Stateful host contract; it never owns or terminates the Desktop app."""

    host_kind = "codex_desktop"
    harness = "codex"

    def __init__(self) -> None:
        self._registrations: dict[str, dict[str, Any]] = {}
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self._heartbeat_ids: dict[tuple[str, str], dict[str, Any]] = {}
        self._admitted_tickets: dict[str, dict[str, Any]] = {}
        self._ticket_acks: dict[str, dict[str, Any]] = {}
        self._ticket_dispatch: dict[str, dict[str, Any]] = {}
        self._joins: dict[str, dict[str, Any]] = {}
        self._run_children: dict[str, set[str]] = {}
        self._run_leases: dict[str, set[str]] = {}
        self._cleanup_receipts: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "{}(host_kind={!r}, raw_auth_stored=False)".format(
            type(self).__name__, self.host_kind
        )

    def register_host(
        self,
        *,
        host_id: str,
        capabilities: tuple[str, ...] | list[str],
        automation_mode: str = "service_callable",
        auth_mode: str = "host_owned",
        heartbeat_ttl_seconds: int = 30,
        host_session_id: str = "",
        now_iso: str = "",
    ) -> dict[str, Any]:
        normalized_host_id = _safe_text(host_id, "host_id")
        normalized_kind = _safe_text(self.host_kind, "host_kind")
        if normalized_kind not in _HOST_KINDS:
            raise DesktopHostAdapterError("Desktop host kind is unsupported")
        mode = _safe_text(automation_mode, "automation_mode")
        if mode not in _AUTOMATION_MODES:
            raise DesktopHostAdapterError("Desktop host automation mode is unsupported")
        normalized_capabilities = tuple(
            sorted(
                {
                    _safe_text(item, "capability")
                    for item in capabilities
                    if str(item or "").strip()
                }
            )
        )
        if not normalized_capabilities:
            raise DesktopHostAdapterError("Desktop host capabilities are required")
        try:
            ttl = int(heartbeat_ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise DesktopHostAdapterError("Desktop heartbeat TTL is invalid") from exc
        if ttl < 5 or ttl > 3600:
            raise DesktopHostAdapterError("Desktop heartbeat TTL is out of range")
        _, registered_at = _timestamp(now_iso)
        immutable = {
            "host_id": normalized_host_id,
            "host_kind": normalized_kind,
            "capabilities": list(normalized_capabilities),
            "automation_mode": mode,
            "auth_mode": _safe_text(auth_mode, "auth_mode"),
            "heartbeat_ttl_seconds": ttl,
            "host_session_id": _safe_text(
                host_session_id, "host_session_id", required=False
            ),
        }
        capability_hash = _stable_hash(
            {
                "host_kind": normalized_kind,
                "capabilities": list(normalized_capabilities),
                "automation_mode": mode,
            }
        )
        registration_hash = _stable_hash(immutable)
        registration = {
            "schema_version": DESKTOP_HOST_REGISTRATION_SCHEMA_VERSION,
            "status": "registered",
            **immutable,
            "registration_id": _stable_ref("dhostreg", immutable),
            "registration_ref": _stable_ref("dhostreg", immutable),
            "registration_hash": registration_hash,
            "capability_hash": capability_hash,
            "registered_at": registered_at,
            "host_owned": True,
            "raw_credential_material_exposed": False,
            "raw_worker_auth_exposed": False,
            "public_safe": True,
        }
        with self._lock:
            existing = self._registrations.get(normalized_host_id)
            if existing is not None:
                if existing["registration_hash"] != registration_hash:
                    raise DesktopHostAdapterError(
                        "Desktop host immutable registration already exists"
                    )
                return deepcopy(existing)
            self._registrations[normalized_host_id] = registration
        return deepcopy(registration)

    register = register_host

    def heartbeat(
        self,
        *,
        host_id: str,
        heartbeat_id: str = "",
        capabilities: tuple[str, ...] | list[str] = (),
        now_iso: str = "",
    ) -> dict[str, Any]:
        normalized_host_id = _safe_text(host_id, "host_id")
        now, observed_at = _timestamp(now_iso)
        with self._lock:
            registration = self._registrations.get(normalized_host_id)
            if registration is None:
                raise DesktopHostAdapterError("Desktop host is not registered")
            supplied_capabilities = tuple(
                sorted(
                    {
                        _safe_text(item, "capability")
                        for item in capabilities
                        if str(item or "").strip()
                    }
                )
            )
            if supplied_capabilities and list(supplied_capabilities) != registration[
                "capabilities"
            ]:
                raise DesktopHostAdapterError(
                    "Desktop heartbeat capabilities do not match registration"
                )
            normalized_heartbeat_id = _safe_text(
                heartbeat_id, "heartbeat_id", required=False
            ) or _stable_ref(
                "dhostbeat",
                {
                    "host_id": normalized_host_id,
                    "registration_hash": registration["registration_hash"],
                    "observed_at": observed_at,
                },
            )
            idempotency_key = (normalized_host_id, normalized_heartbeat_id)
            previous = self._heartbeat_ids.get(idempotency_key)
            if previous is not None:
                return deepcopy(previous)
            expires_at = (
                now + timedelta(seconds=registration["heartbeat_ttl_seconds"])
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
            heartbeat = {
                "schema_version": DESKTOP_HOST_HEARTBEAT_SCHEMA_VERSION,
                "status": "healthy",
                "host_id": normalized_host_id,
                "host_kind": registration["host_kind"],
                "registration_id": registration["registration_id"],
                "registration_hash": registration["registration_hash"],
                "capability_hash": registration["capability_hash"],
                "heartbeat_id": normalized_heartbeat_id,
                "heartbeat_ref": "desktop-heartbeat:{}".format(
                    normalized_heartbeat_id
                ),
                "observed_at": observed_at,
                "expires_at": expires_at,
                "raw_credential_material_exposed": False,
                "raw_worker_auth_exposed": False,
                "public_safe": True,
            }
            self._heartbeat_ids[idempotency_key] = heartbeat
            self._heartbeats[normalized_host_id] = heartbeat
        return deepcopy(heartbeat)

    record_heartbeat = heartbeat

    def host_capabilities(self, host_id: str, *, now_iso: str = "") -> dict[str, Any]:
        normalized_host_id = _safe_text(host_id, "host_id")
        now, _ = _timestamp(now_iso)
        with self._lock:
            registration = self._registrations.get(normalized_host_id)
            if registration is None:
                raise DesktopHostAdapterError("Desktop host is not registered")
            heartbeat = self._heartbeats.get(normalized_host_id)
            heartbeat_current = False
            if heartbeat is not None:
                expiry, _ = _timestamp(heartbeat["expires_at"])
                heartbeat_current = expiry >= now
            return {
                "schema_version": "cli_agent_service.desktop_host_capabilities.v1",
                "status": "ready" if heartbeat_current else "heartbeat_required",
                "host_id": normalized_host_id,
                "host_kind": registration["host_kind"],
                "capabilities": list(registration["capabilities"]),
                "automation_mode": registration["automation_mode"],
                "auth_mode": registration["auth_mode"],
                "registration_id": registration["registration_id"],
                "registration_hash": registration["registration_hash"],
                "capability_hash": registration["capability_hash"],
                "heartbeat_current": heartbeat_current,
                "heartbeat_ref": str(
                    (heartbeat or {}).get("heartbeat_ref") or ""
                ),
                "public_safe": True,
                "raw_worker_auth_exposed": False,
            }

    capabilities = host_capabilities

    def admit_execution_ticket(
        self,
        *,
        execution_ticket: Mapping[str, Any],
        canonical_execution_ticket: Mapping[str, Any],
        now_iso: str = "",
    ) -> dict[str, Any]:
        """Refuse caller-side admission; the owning service must rederive it."""

        del execution_ticket, canonical_execution_ticket, now_iso
        raise DesktopHostAdapterError(
            "execution ticket admission requires canonical service authority"
        )

    def _admit_service_execution_ticket(
        self,
        *,
        canonical_execution_ticket: Mapping[str, Any],
        now_iso: str = "",
    ) -> dict[str, Any]:
        """Admit only the ticket returned by the service's trusted resolver."""

        ticket, dispatch = _validated_issued_ticket(
            canonical_execution_ticket
        )
        profile = ticket.get("profile_requirements")
        if not isinstance(profile, Mapping):
            raise DesktopHostAdapterError(
                "execution ticket profile is missing"
            )
        ticket_harness = str(profile.get("harness") or "").strip().lower()
        if ticket_harness and ticket_harness != self.harness:
            raise DesktopHostAdapterError(
                "execution ticket harness does not match Desktop host"
            )
        _, admitted_at = _timestamp(now_iso)
        ticket_id = ticket["ticket_id"]
        ticket_hash = ticket["ticket_hash"]
        admission_seed = {
            "ticket_id": ticket_id,
            "immutable_ticket_hash": ticket_hash,
            "contract_execution_id": ticket["contract_execution_id"],
            "execution_state_revision": ticket["execution_state_revision"],
            "execution_state_hash": ticket["execution_state_hash"],
            "authority_decision_source": ticket["authority_decision_source"],
            "dispatch_identity_hash": ticket["dispatch_identity_hash"],
        }
        admission = {
            "schema_version": DESKTOP_EXECUTION_TICKET_ADMISSION_SCHEMA_VERSION,
            "status": "admitted",
            **admission_seed,
            "ticket_admission_id": _stable_ref("dhostticket", admission_seed),
            "ticket_admission_hash": _stable_hash(admission_seed),
            "admitted_at": admitted_at,
            "canonical_rederivation_verified": True,
            "service_owned_authority_path": True,
            "raw_worker_auth_exposed": False,
            "public_safe": True,
        }
        with self._lock:
            existing = self._admitted_tickets.get(ticket_id)
            if existing is not None:
                if existing["ticket"] != ticket:
                    raise DesktopHostAdapterError(
                        "execution ticket id is already bound to another immutable ticket"
                    )
                return deepcopy(existing["admission"])
            self._admitted_tickets[ticket_id] = {
                "ticket": ticket,
                "dispatch_identity": dispatch,
                "admission": admission,
            }
        return deepcopy(admission)

    def acknowledge_execution_ticket(
        self,
        *,
        host_id: str,
        execution_ticket: Mapping[str, Any],
        run_id: str = "",
        now_iso: str = "",
    ) -> dict[str, Any]:
        normalized_host_id = _safe_text(host_id, "host_id")
        ticket, dispatch = _validated_issued_ticket(execution_ticket)
        ticket_id = ticket["ticket_id"]
        ticket_hash = ticket["ticket_hash"]
        profile = ticket.get("profile_requirements")
        if not isinstance(profile, Mapping):
            raise DesktopHostAdapterError("execution ticket profile is missing")
        ticket_harness = str(profile.get("harness") or "").strip().lower()
        if ticket_harness and ticket_harness != self.harness:
            raise DesktopHostAdapterError(
                "execution ticket harness does not match Desktop host"
            )
        run = _safe_text(run_id, "run_id", required=False) or _stable_ref(
            "desktop-run", {"ticket_id": ticket_id, "ticket_hash": ticket_hash}
        )
        _, acknowledged_at = _timestamp(now_iso)
        with self._lock:
            admitted = self._admitted_tickets.get(ticket_id)
            if admitted is None or admitted["ticket"] != ticket:
                raise DesktopHostAdapterError(
                    "execution ticket was not admitted by canonical service authority"
                )
            capabilities = self.host_capabilities(
                normalized_host_id, now_iso=now_iso
            )
            if not capabilities["heartbeat_current"]:
                raise DesktopHostAdapterError(
                    "Desktop host heartbeat is stale or missing"
                )
            existing = self._ticket_acks.get(ticket_id)
            if existing is not None:
                if (
                    existing["host_id"] != normalized_host_id
                    or existing["ticket_hash"] != ticket_hash
                    or existing["dispatch_identity_hash"]
                    != ticket["dispatch_identity_hash"]
                    or existing["run_id"] != run
                ):
                    raise DesktopHostAdapterError(
                        "execution ticket was already acknowledged for another run"
                    )
                return deepcopy(existing)
            registration = self._registrations[normalized_host_id]
            heartbeat = self._heartbeats[normalized_host_id]
            admission = admitted["admission"]
            ack_seed = {
                "host_id": normalized_host_id,
                "registration_hash": registration["registration_hash"],
                "heartbeat_ref": heartbeat["heartbeat_ref"],
                "ticket_admission_hash": admission["ticket_admission_hash"],
                "ticket_id": ticket_id,
                "immutable_ticket_hash": ticket_hash,
                "contract_execution_id": ticket["contract_execution_id"],
                "execution_state_revision": ticket["execution_state_revision"],
                "execution_state_hash": ticket["execution_state_hash"],
                "authority_decision_source": ticket["authority_decision_source"],
                "dispatch_identity_hash": ticket["dispatch_identity_hash"],
                "run_id": run,
            }
            ack = {
                "schema_version": DESKTOP_EXECUTION_TICKET_ACK_SCHEMA_VERSION,
                "status": "acknowledged",
                "host_id": normalized_host_id,
                "host_kind": self.host_kind,
                "automation_mode": registration["automation_mode"],
                "run_id": run,
                "ticket_id": ticket_id,
                "ticket_hash": ticket_hash,
                "immutable_ticket_hash": ticket_hash,
                "ticket_admission_id": admission["ticket_admission_id"],
                "ticket_admission_hash": admission["ticket_admission_hash"],
                "contract_execution_id": ticket["contract_execution_id"],
                "execution_state_revision": ticket["execution_state_revision"],
                "execution_state_hash": ticket["execution_state_hash"],
                "authority_decision_source": ticket["authority_decision_source"],
                "ticket_ack_id": _stable_ref("dhostack", ack_seed),
                "ticket_ack_hash": _stable_hash(ack_seed),
                "registration_id": registration["registration_id"],
                "registration_hash": registration["registration_hash"],
                "capability_hash": registration["capability_hash"],
                "heartbeat_ref": heartbeat["heartbeat_ref"],
                "dispatch_identity": dispatch,
                "dispatch_identity_hash": ticket["dispatch_identity_hash"],
                "acknowledged_at": acknowledged_at,
                "spawn_authorized": bool(
                    registration["automation_mode"] == "service_callable"
                ),
                "spawn_claimed": False,
                "contract_runtime_authority_preserved": True,
                "merge_authority": False,
                "close_authority": False,
                "raw_worker_auth_exposed": False,
                "public_safe": True,
            }
            self._ticket_acks[ticket_id] = ack
            self._ticket_dispatch[ticket_id] = dispatch
        return deepcopy(ack)

    acknowledge_ticket = acknowledge_execution_ticket

    def prepare_handoff(
        self,
        *,
        ticket_ack: Mapping[str, Any],
        worker_envelope: Mapping[str, Any],
        launch_text_hash: str,
        launch_text_ref: str = "response.launch_text",
    ) -> dict[str, Any]:
        ack = self._verified_ack(ticket_ack)
        envelope = _copy_safe_worker_envelope(worker_envelope)
        if envelope["runtime_context_id"] != ack["dispatch_identity"][
            "runtime_context_id"
        ]:
            raise DesktopHostAdapterError(
                "worker envelope runtime context does not match execution ticket"
            )
        normalized_launch_hash = _safe_text(
            launch_text_hash, "launch_text_hash"
        )
        if not _HASH.fullmatch(normalized_launch_hash):
            raise DesktopHostAdapterError("launch_text_hash must be sha256 content")
        return {
            "schema_version": CODEX_DESKTOP_HANDOFF_SCHEMA_VERSION,
            "status": "ready_for_host_builtin_agent",
            "host_id": ack["host_id"],
            "host_kind": self.host_kind,
            "automation_mode": ack["automation_mode"],
            "run_id": ack["run_id"],
            "ticket_id": ack["ticket_id"],
            "ticket_hash": ack["ticket_hash"],
            "ticket_ack_id": ack["ticket_ack_id"],
            "ticket_ack_hash": ack["ticket_ack_hash"],
            "action": "host_create_builtin_agent",
            "agent_spawned": False,
            "spawn_claimed": False,
            "copy_safe_worker_envelope": envelope,
            "launch_text": {
                "source_ref": _safe_text(launch_text_ref, "launch_text_ref"),
                "sha256": normalized_launch_hash,
                "body_included": False,
            },
            "raw_worker_auth_delivery": "desktop_host_private",
            "raw_worker_auth_exposed": False,
            "raw_worker_auth_persisted": False,
            "private_prompt_body_included": False,
            "contract_runtime_authority_preserved": True,
            "merge_authority": False,
            "close_authority": False,
            "public_safe": True,
        }

    build_handoff = prepare_handoff

    def _verified_ack(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise DesktopHostAdapterError("execution ticket acknowledgement is required")
        ticket_id = _safe_text(value.get("ticket_id"), "ticket_id")
        with self._lock:
            stored = self._ticket_acks.get(ticket_id)
            if stored is None:
                raise DesktopHostAdapterError(
                    "execution ticket acknowledgement is not registered"
                )
            if dict(value) != stored:
                raise DesktopHostAdapterError(
                    "execution ticket acknowledgement does not match registration"
                )
            return deepcopy(stored)

    def join_runtime_context(
        self,
        *,
        ticket_ack: Mapping[str, Any],
        actual_host_worker_id: str,
        worker_session_id: str,
        worker_transcript_ref: str,
        session_token_ref: str,
        observer_command_id: str,
        launch_text_hash: str,
        worker_slot_id: str = "",
        host_startup_id: str = "",
        now_iso: str = "",
    ) -> dict[str, Any]:
        ack = self._verified_ack(ticket_ack)
        capabilities = self.host_capabilities(ack["host_id"], now_iso=now_iso)
        if not capabilities["heartbeat_current"]:
            raise DesktopHostAdapterError("Desktop host heartbeat is stale or missing")
        worker_id = _safe_text(actual_host_worker_id, "actual_host_worker_id")
        session_id = _safe_text(worker_session_id, "worker_session_id")
        transcript_ref = _safe_text(worker_transcript_ref, "worker_transcript_ref")
        token_ref = _safe_text(session_token_ref, "session_token_ref")
        if not token_ref.startswith("wstok-"):
            raise DesktopHostAdapterError("session_token_ref is not copy-safe worker auth")
        command_id = _safe_text(observer_command_id, "observer_command_id")
        normalized_launch_hash = _safe_text(launch_text_hash, "launch_text_hash")
        if not _HASH.fullmatch(normalized_launch_hash):
            raise DesktopHostAdapterError("launch_text_hash must be sha256 content")
        dispatch = ack["dispatch_identity"]
        canonical_command_id = _safe_text(
            dispatch.get("observer_command_id"),
            "execution ticket observer_command_id",
        )
        if command_id != canonical_command_id:
            raise DesktopHostAdapterError(
                "observer_command_id does not match execution ticket"
            )
        canonical_slot_id = _safe_text(
            dispatch.get("worker_slot_id"),
            "execution ticket worker_slot_id",
        )
        supplied_slot_id = _safe_text(
            worker_slot_id,
            "worker_slot_id",
            required=False,
        )
        if supplied_slot_id and supplied_slot_id != canonical_slot_id:
            raise DesktopHostAdapterError(
                "worker_slot_id does not match execution ticket"
            )
        slot_id = canonical_slot_id
        startup_id = _safe_text(
            host_startup_id, "host_startup_id", required=False
        ) or _stable_ref(
            "desktop-startup",
            {
                "host_id": ack["host_id"],
                "ticket_ack_hash": ack["ticket_ack_hash"],
                "actual_host_worker_id": worker_id,
                "worker_session_id": session_id,
            },
        )
        join_seed = {
            "host_id": ack["host_id"],
            "ticket_ack_hash": ack["ticket_ack_hash"],
            "ticket_admission_hash": ack["ticket_admission_hash"],
            "immutable_ticket_hash": ack["immutable_ticket_hash"],
            "contract_execution_id": ack["contract_execution_id"],
            "execution_state_revision": ack["execution_state_revision"],
            "execution_state_hash": ack["execution_state_hash"],
            "authority_decision_source": ack["authority_decision_source"],
            "dispatch_identity_hash": ack["dispatch_identity_hash"],
            "runtime_context_id": dispatch["runtime_context_id"],
            "task_id": dispatch["task_id"],
            "actual_host_worker_id": worker_id,
            "worker_session_id": session_id,
            "worker_transcript_ref": transcript_ref,
            "session_token_ref": token_ref,
            "host_startup_id": startup_id,
        }
        join_id = _stable_ref("dhostjoin", join_seed)
        join_hash = _stable_hash(join_seed)
        registration = self._registrations[ack["host_id"]]
        heartbeat = self._heartbeats[ack["host_id"]]
        try:
            from governance.parallel_branch_runtime import (
                build_registered_host_adapter_spawn_identity,
            )
        except ImportError:  # pragma: no cover - package import path
            from ...governance.parallel_branch_runtime import (
                build_registered_host_adapter_spawn_identity,
            )

        registered_identity = build_registered_host_adapter_spawn_identity(
            project_id=str(dispatch.get("project_id") or ""),
            runtime_context_id=dispatch["runtime_context_id"],
            observer_command_id=command_id,
            launch_text_hash=normalized_launch_hash,
            backend_mode=self.host_kind,
            startup_source="{}_governed_join".format(self.host_kind),
            task_id=dispatch["task_id"],
            worker_slot_id=str(
                slot_id
            ),
            agent_id=worker_id,
            actual_host_worker_id=worker_id,
            host_startup_id=startup_id,
            host_session_id=session_id,
            session_token_surrogate="",
        )
        registered_identity.pop("session_token_surrogate", None)
        registered_identity.update(
            {
                "worker_id": str(dispatch.get("worker_id") or ""),
                "session_token_ref": token_ref,
                "host_id": ack["host_id"],
                "host_kind": self.host_kind,
                "host_registration_id": registration["registration_id"],
                "host_registration_hash": registration["registration_hash"],
                "host_capability_hash": registration["capability_hash"],
                "host_heartbeat_ref": heartbeat["heartbeat_ref"],
                "execution_ticket_id": ack["ticket_id"],
                "execution_ticket_hash": ack["ticket_hash"],
                "immutable_ticket_hash": ack["immutable_ticket_hash"],
                "ticket_admission_id": ack["ticket_admission_id"],
                "ticket_admission_hash": ack["ticket_admission_hash"],
                "contract_execution_id": ack["contract_execution_id"],
                "execution_state_revision": ack["execution_state_revision"],
                "execution_state_hash": ack["execution_state_hash"],
                "authority_decision_source": ack["authority_decision_source"],
                "dispatch_identity_hash": ack["dispatch_identity_hash"],
                "execution_ticket_ack_id": ack["ticket_ack_id"],
                "execution_ticket_ack_hash": ack["ticket_ack_hash"],
                "runtime_join_id": join_id,
                "runtime_join_hash": join_hash,
                "automation_mode": registration["automation_mode"],
                "auth_mode": registration["auth_mode"],
            }
        )
        _, joined_at = _timestamp(now_iso)
        joined = {
            "schema_version": DESKTOP_RUNTIME_JOIN_SCHEMA_VERSION,
            "status": "joined",
            "host_id": ack["host_id"],
            "host_kind": self.host_kind,
            "run_id": ack["run_id"],
            "ticket_id": ack["ticket_id"],
            "ticket_hash": ack["ticket_hash"],
            "immutable_ticket_hash": ack["immutable_ticket_hash"],
            "ticket_admission_id": ack["ticket_admission_id"],
            "ticket_admission_hash": ack["ticket_admission_hash"],
            "contract_execution_id": ack["contract_execution_id"],
            "execution_state_revision": ack["execution_state_revision"],
            "execution_state_hash": ack["execution_state_hash"],
            "authority_decision_source": ack["authority_decision_source"],
            "dispatch_identity_hash": ack["dispatch_identity_hash"],
            "ticket_ack_id": ack["ticket_ack_id"],
            "ticket_ack_hash": ack["ticket_ack_hash"],
            "runtime_join_id": join_id,
            "runtime_join_hash": join_hash,
            "runtime_context_id": dispatch["runtime_context_id"],
            "task_id": dispatch["task_id"],
            "worker_id": dispatch["worker_id"],
            "worker_slot_id": dispatch["worker_slot_id"],
            "observer_command_id": dispatch["observer_command_id"],
            "parent_task_id": str(dispatch.get("parent_task_id") or ""),
            "target_project_root": dispatch["target_project_root"],
            "actual_host_worker_id": worker_id,
            "worker_session_id": session_id,
            "worker_transcript_ref": transcript_ref,
            "session_token_ref": token_ref,
            "host_startup_id": startup_id,
            "registered_host_adapter_spawn": registered_identity,
            "joined_at": joined_at,
            "server_verification_required": True,
            "startup_close_satisfying_claimed": False,
            "raw_worker_auth_exposed": False,
            "raw_worker_auth_persisted": False,
            "public_safe": True,
        }
        with self._lock:
            if ack["run_id"] in self._cleanup_receipts:
                raise DesktopHostAdapterError(
                    "Desktop run was already cleaned and cannot accept a worker join"
                )
            existing = self._joins.get(ack["ticket_id"])
            if existing is not None:
                if existing["runtime_join_hash"] != join_hash:
                    raise DesktopHostAdapterError(
                        "execution ticket is already joined to another Desktop worker"
                    )
                return deepcopy(existing)
            self._joins[ack["ticket_id"]] = joined
            self._run_children.setdefault(ack["run_id"], set()).add(worker_id)
            self._run_leases.setdefault(ack["run_id"], set()).add(ack["ticket_id"])
        return deepcopy(joined)

    join = join_runtime_context

    def cleanup_run(self, run_id: str, *, reason: str = "run_cleanup") -> dict[str, Any]:
        normalized_run_id = _safe_text(run_id, "run_id")
        normalized_reason = _safe_text(reason, "cleanup reason")
        with self._lock:
            existing = self._cleanup_receipts.get(normalized_run_id)
            if existing is not None:
                return deepcopy(existing)
            child_count = len(self._run_children.pop(normalized_run_id, set()))
            lease_count = len(self._run_leases.pop(normalized_run_id, set()))
            receipt = {
                "schema_version": DESKTOP_RUN_CLEANUP_SCHEMA_VERSION,
                "status": "cleaned",
                "run_id": normalized_run_id,
                "reason": normalized_reason,
                "run_children_released": child_count,
                "run_leases_released": lease_count,
                "cleanup_scope": "per_run_children_and_leases_only",
                "desktop_host_action": "none",
                "desktop_host_terminated": False,
                "desktop_host_relaunched": False,
                "desktop_host_restart_requested": False,
                "raw_worker_auth_exposed": False,
                "public_safe": True,
            }
            self._cleanup_receipts[normalized_run_id] = receipt
            return deepcopy(receipt)

    cleanup = cleanup_run
    cancel_run = cleanup_run


__all__ = (
    "CODEX_DESKTOP_HANDOFF_SCHEMA_VERSION",
    "DESKTOP_EXECUTION_TICKET_ACK_SCHEMA_VERSION",
    "DESKTOP_EXECUTION_TICKET_ADMISSION_SCHEMA_VERSION",
    "DESKTOP_HOST_HEARTBEAT_SCHEMA_VERSION",
    "DESKTOP_HOST_REGISTRATION_SCHEMA_VERSION",
    "DESKTOP_RUN_CLEANUP_SCHEMA_VERSION",
    "DESKTOP_RUNTIME_JOIN_SCHEMA_VERSION",
    "CodexDesktopAdapter",
    "DesktopHostAdapterError",
)
