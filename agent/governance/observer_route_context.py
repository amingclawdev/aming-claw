"""Aming-owned, write-authorizing observer route-context / route-token generator.

This module mints a legitimate write-authorizing observer route token WITHOUT
depending on any external route provider (e.g. the judgment-brain plugin) at
runtime. Today aming-claw only VALIDATES route tokens
(``mf_subagent_contract.validate_route_token_mutation_gate``) and its only native
route-context builder (``observer_repair_run._build_route_context``) is read-only
(``authorizes_protected_write=False``). This generator is the native, owned
counterpart that produces a token which passes
``validate_route_token_mutation_gate`` with ``decision == "route_token"`` for the
observer's protected write actions (e.g. ``task_timeline_append``,
``backlog_close``) when scope matches ``{project_id, backlog_id, task_id}``.

Security-sensitive: the token's ``allowed_actions`` intentionally excludes
direct file-edit / patch / implementation actions; those remain in
``blocked_actions``. The observer is only authorized to capture intent, dispatch
bounded subagents, verify, and merge/close after worker + independent QA
evidence. Caller-supplied ``allowed_actions`` are sanitized at mint (wildcard and
any blocked-action intersection are rejected) and ``ttl_hours`` is clamped to a
maximum so a leaked token has a bounded blast radius.

Provider independence: this module does NOT import or call any external route
provider at import time or runtime. ``resolve_route_provider`` only *records*
whether an external provider is declared in project config; when one is declared
the caller is told to route issuance through it, but this module never invokes
it. When none is declared, the aming-local default (``owner="aming-claw"``) is
used.

Consumability: ``issue_observer_write_route_context`` returns, in addition to the
token, a deterministic opaque ``route_token_ref`` (the raw token text is never
embedded in clear in the ref) and a ``merge_queue_id``, plus a ready
``execute_backlog_row_payload`` carrying exactly the fields the observer
``execute_backlog_row`` command requires.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "aming_observer_write_route_token.v1"
PROVIDER_SCHEMA_VERSION = "aming_observer_route_provider.v1"
ISSUE_SCHEMA_VERSION = "aming_observer_write_route_issue.v1"
OWNER = "aming-claw"
CALLER_ROLE = "observer"
TOPOLOGY = "observer_owned_write_route"

# Maximum lifetime (hours) a minted write-authorizing route token may request.
# Caller-supplied ``ttl_hours`` is rejected (not silently clamped) when it
# exceeds this bound or is non-positive, to cap the blast radius of a leaked
# token. Default remains 24h.
MAX_TTL_HOURS: float = 72.0

# Default protected actions the observer route token authorizes. These are
# orchestration / evidence / close actions — NOT direct file mutation.
DEFAULT_ALLOWED_ACTIONS: tuple[str, ...] = (
    "task_timeline_append",
    "backlog_upsert",
    "backlog_close",
    "capture_intent",
    "run_route_precheck",
    "dispatch_bounded_lane",
    "verify_evidence",
    "close_or_merge_after_evidence",
    "execute_backlog_row",
)

# Actions the observer must NEVER perform directly via this token. Direct file
# edits / patches / implementation must go through a bounded subagent, and a
# close must never happen without worker + independent subagent evidence.
BLOCKED_ACTIONS: tuple[str, ...] = (
    "edit_files",
    "edit_file",
    "apply_patch",
    "apply_patch_within_target_files",
    "write_file",
    "write_files",
    "mutate_files",
    "run_implementation_command",
    "implementation_file_edit",
    "close_without_worker_or_subagent_evidence",
)

REQUIRED_LANES: tuple[dict[str, str], ...] = (
    {
        "id": "observer_intent_capture",
        "role": "observer",
        "purpose": "capture the requirement and record route context consumption",
    },
    {
        "id": "bounded_implementation_subagent",
        "role": "mf_sub",
        "purpose": "perform file edits inside the fenced target_files only",
    },
    {
        "id": "independent_verification_subagent",
        "role": "mf_sub",
        "purpose": "independently verify implementation evidence and run tests",
    },
    {
        "id": "observer_merge_close_gate",
        "role": "observer",
        "purpose": "merge and close only after worker + verification evidence passes",
    },
)

REQUIRED_EVIDENCE: tuple[str, ...] = (
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_or_hash",
    "caller_role",
    "allowed_actions",
    "blocked_actions",
    "required_lanes",
    "bounded_implementation_subagent_id",
    "independent_verification_subagent_id",
    "verification_report",
    "dirty_scope_check",
)


def _stable_digest(value: Any, *, length: int = 16) -> str:
    """Deterministic hex digest of a JSON-canonicalized value.

    Mirrors ``observer_repair_run._stable_hash`` style (sorted keys, compact
    separators, ``default=str``) so identical inputs yield identical identity.
    """
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _sha256(value: Any) -> str:
    return "sha256:" + _stable_digest(value, length=64)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        token = value.strip()
        return [token] if token else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    out: list[str] = []
    for item in value:
        token = _string(item)
        if token:
            out.append(token)
    return out


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        token = _string(value)
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def resolve_route_provider(
    project_id: str,
    *,
    root: Path | str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the configured route provider for *project_id*.

    Returns provider evidence recorded into the minted token. When an external
    provider is declared in project config (``ai.routing.route`` /
    ``ai.routing.observer_route`` / ``ai.routing.route_provider``), it is
    *recorded* (source ``external_provider``) with its id/version/hash — this
    function does NOT call the external provider; it only notes that one is
    configured and that the caller should route issuance through it. When none
    is declared, returns the aming-local default
    (``source="aming_local_default"``, ``owner="aming-claw"``).

    ``config`` may be injected directly (used by tests / callers that already
    loaded project config); otherwise project config is loaded from ``root``.
    Loading is best-effort and never imports an external route provider.
    """
    project_id = _string(project_id)
    declared: Mapping[str, Any] | None = None
    config_source = "none"

    raw = config
    if raw is None and root is not None:
        try:
            from project_config import load_project_config  # type: ignore

            loaded = load_project_config(Path(root))
            routing = getattr(getattr(loaded, "ai", None), "routing", None)
            raw = routing if isinstance(routing, Mapping) else None
            config_source = "project_config"
        except Exception:
            raw = None
            config_source = "project_config_unavailable"
    elif raw is not None:
        config_source = "injected"

    if isinstance(raw, Mapping):
        for key in ("route", "observer_route", "route_provider"):
            candidate = raw.get(key)
            if isinstance(candidate, Mapping) and (
                candidate.get("provider") or candidate.get("id")
            ):
                declared = candidate
                break

    if declared:
        provider_id = _string(declared.get("id") or declared.get("provider"))
        version = _string(declared.get("version")) or "unspecified"
        descriptor = {
            "id": provider_id,
            "version": version,
            "model": _string(declared.get("model")),
        }
        return {
            "schema_version": PROVIDER_SCHEMA_VERSION,
            "source": "external_provider",
            "owner": provider_id or "external",
            "external_provider_required": False,
            "judgment_brain_required": False,
            "id": provider_id,
            "version": version,
            "model": _string(declared.get("model")),
            "hash": _sha256(descriptor),
            "config_source": config_source,
            "note": (
                "external route provider configured; caller should route token "
                "issuance through it. it is not invoked here."
            ),
        }

    descriptor = {"id": "aming-local-default", "owner": OWNER, "project_id": project_id}
    return {
        "schema_version": PROVIDER_SCHEMA_VERSION,
        "source": "aming_local_default",
        "owner": OWNER,
        "external_provider_required": False,
        "judgment_brain_required": False,
        "id": "aming-local-default",
        "version": SCHEMA_VERSION,
        "model": "",
        "hash": _sha256(descriptor),
        "config_source": config_source,
        "note": "no external route provider configured; using aming-claw native default.",
    }


def _sanitize_allowed_actions(
    allowed_actions: Sequence[str] | None,
) -> list[str]:
    """Resolve + sanitize the token's allowed actions.

    When the caller supplies ``allowed_actions`` they are sanitized at mint: the
    wildcard ``"*"`` and any value intersecting ``BLOCKED_ACTIONS`` are rejected
    with ``ValueError``. The downstream gate
    (``mf_subagent_contract.validate_route_token_mutation_gate``) only checks
    ``allowed_actions`` membership and IGNORES ``blocked_actions``, so an
    unsanitized ``["*"]`` or ``["edit_files"]`` token would be a privilege
    over-reach; this is the choke point that prevents it on both the HTTP and
    MCP issuance paths.
    """
    caller_supplied = allowed_actions is not None
    actions = list(allowed_actions) if allowed_actions else list(DEFAULT_ALLOWED_ACTIONS)
    actions_list = _dedupe(_string_list(actions))
    if not actions_list:
        raise ValueError("allowed_actions must be non-empty")

    if caller_supplied:
        if "*" in actions_list:
            raise ValueError('allowed_actions must not contain the wildcard "*"')
        overreach = sorted(set(actions_list) & set(BLOCKED_ACTIONS))
        if overreach:
            raise ValueError(
                "allowed_actions must not include blocked actions: "
                + ", ".join(overreach)
            )
    return actions_list


def build_observer_write_route_token(
    *,
    project_id: str,
    backlog_id: str,
    task_id: str,
    target_files: Sequence[str],
    allowed_actions: Sequence[str] | None = None,
    ttl_hours: float = 24.0,
    now: datetime | None = None,
    provider: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[str] | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Mint an Aming-owned, write-authorizing observer route token.

    The returned dict is accepted by
    ``mf_subagent_contract.validate_route_token_mutation_gate`` with
    ``decision == "route_token"`` for the listed ``allowed_actions`` when the
    request scope matches ``{project_id, backlog_id, task_id}``.

    Deterministic: identical inputs (project/backlog/task, sorted target_files,
    sorted allowed_actions, date) produce identical route identity hashes. An
    injectable ``now`` keeps ``expires_at`` deterministic for tests.
    """
    project_id = _string(project_id)
    backlog_id = _string(backlog_id)
    task_id = _string(task_id)
    if not project_id:
        raise ValueError("project_id is required")
    if not backlog_id:
        raise ValueError("backlog_id is required")
    if not task_id:
        raise ValueError("task_id is required")

    target_files_list = sorted(_dedupe(_string_list(target_files)))
    if not target_files_list:
        raise ValueError("target_files must be a non-empty list of file paths")

    actions_list = _sanitize_allowed_actions(allowed_actions)

    # Reject oversized / non-positive TTL explicitly (do not silently clamp).
    ttl_hours = float(ttl_hours)
    if ttl_hours <= 0:
        raise ValueError("ttl_hours must be > 0")
    if ttl_hours > MAX_TTL_HOURS:
        raise ValueError(f"ttl_hours must not exceed MAX_TTL_HOURS ({MAX_TTL_HOURS})")

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)
    expires_dt = now_dt + timedelta(hours=ttl_hours)

    # Provider evidence — independent of any external route provider.
    provider_evidence = (
        dict(provider)
        if isinstance(provider, Mapping)
        else resolve_route_provider(project_id, root=project_root)
    )

    # Deterministic identity base. Date (not full timestamp) so same inputs on
    # the same day yield the same route identity; the live expires_at carries
    # the precise expiry.
    date_str = now_dt.strftime("%Y%m%d")
    identity_base = {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER,
        "project_id": project_id,
        "backlog_id": backlog_id,
        "task_id": task_id,
        "target_files": target_files_list,
        "allowed_actions": sorted(actions_list),
        "blocked_actions": sorted(BLOCKED_ACTIONS),
        "caller_role": CALLER_ROLE,
        "date": date_str,
        "provider_hash": provider_evidence.get("hash", ""),
    }
    digest = _stable_digest(identity_base, length=16)
    route_id = f"route-{date_str}-{digest}"
    route_context_hash = _sha256(identity_base)

    prompt_contract_base = {**identity_base, "kind": "prompt_contract"}
    prompt_contract_id = f"rprompt-aming-{_stable_digest(prompt_contract_base, length=16)}"
    prompt_contract_hash = _sha256(prompt_contract_base)

    manifest_base = {
        **identity_base,
        "kind": "visible_injection_manifest",
        "required_lanes": [dict(lane) for lane in REQUIRED_LANES],
        "required_evidence": list(REQUIRED_EVIDENCE),
    }
    visible_injection_manifest_hash = _sha256(manifest_base)

    refs = [f"route:{route_id}"]
    refs.extend(_string_list(evidence_refs))
    evidence_refs_list = _dedupe(refs)

    token: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER,
        "external_provider_required": False,
        "judgment_brain_required": False,
        "authorizes_protected_write": True,
        "read_only": False,
        "topology": TOPOLOGY,
        "route_id": route_id,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "visible_injection_manifest_hash": visible_injection_manifest_hash,
        "caller_role": CALLER_ROLE,
        "allowed_actions": actions_list,
        "blocked_actions": list(BLOCKED_ACTIONS),
        "required_lanes": [dict(lane) for lane in REQUIRED_LANES],
        "required_evidence": list(REQUIRED_EVIDENCE),
        "evidence_refs": evidence_refs_list,
        "expires_at": expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "task_id": task_id,
        },
        "target_files": target_files_list,
        "provider": provider_evidence,
        "issued_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return token


def derive_route_token_ref(token: Mapping[str, Any]) -> str:
    """Derive a deterministic, opaque, consumable reference for a minted token.

    The reference is a stable handle the observer can pass to downstream
    consumers (e.g. an ``execute_backlog_row`` command payload). It is derived
    from the token's PUBLIC route identity only — it never embeds the raw token
    body in clear, so persisting the ref leaks no secret. Two mints of the same
    identity yield the same ref.
    """
    if not isinstance(token, Mapping):
        raise ValueError("token must be a mapping")
    identity = {
        "route_id": _string(token.get("route_id")),
        "route_context_hash": _string(token.get("route_context_hash")),
        "prompt_contract_id": _string(token.get("prompt_contract_id")),
        "visible_injection_manifest_hash": _string(
            token.get("visible_injection_manifest_hash")
        ),
        "scope": dict(token.get("scope") or {}),
    }
    if not identity["route_context_hash"] or not identity["prompt_contract_id"]:
        raise ValueError("token is missing route identity required to derive a ref")
    return "rtok-" + _stable_digest(identity, length=32)


def derive_merge_queue_id(token: Mapping[str, Any]) -> str:
    """Derive a deterministic merge-queue id consumable by execute_backlog_row.

    Bound to the same route identity as the token so it cannot be reused across
    a stale/forked identity. Deterministic for a given identity.
    """
    if not isinstance(token, Mapping):
        raise ValueError("token must be a mapping")
    scope = dict(token.get("scope") or {})
    base = {
        "route_id": _string(token.get("route_id")),
        "route_context_hash": _string(token.get("route_context_hash")),
        "backlog_id": _string(scope.get("backlog_id")),
        "task_id": _string(scope.get("task_id")),
    }
    if not base["route_id"]:
        raise ValueError("token is missing route_id required to derive a merge_queue_id")
    return "mq-" + _stable_digest(base, length=24)


def build_execute_backlog_row_payload(
    token: Mapping[str, Any],
    *,
    route_token_ref: str,
    merge_queue_id: str,
) -> dict[str, Any]:
    """Assemble exactly the route-identity fields an execute_backlog_row needs.

    Mirrors ``observer_session.EXECUTE_BACKLOG_ROW_REQUIRED_PAYLOAD_FIELDS``:
    ``backlog_id``, ``merge_queue_id``, ``route_id``, ``route_context_hash``,
    ``prompt_contract_id``, ``route_token_ref``, ``visible_injection_manifest_hash``.
    The raw token body is intentionally NOT included (only the opaque ref).
    """
    scope = dict(token.get("scope") or {})
    return {
        "backlog_id": _string(scope.get("backlog_id")),
        "task_id": _string(scope.get("task_id")),
        "merge_queue_id": _string(merge_queue_id),
        "route_id": _string(token.get("route_id")),
        "route_context_hash": _string(token.get("route_context_hash")),
        "prompt_contract_id": _string(token.get("prompt_contract_id")),
        "prompt_contract_hash": _string(token.get("prompt_contract_hash")),
        "route_token_ref": _string(route_token_ref),
        "visible_injection_manifest_hash": _string(
            token.get("visible_injection_manifest_hash")
        ),
        "caller_role": CALLER_ROLE,
    }


def issue_observer_write_route_context(
    *,
    project_id: str,
    backlog_id: str,
    task_id: str,
    target_files: Sequence[str],
    allowed_actions: Sequence[str] | None = None,
    ttl_hours: float = 24.0,
    now: datetime | None = None,
    provider: Mapping[str, Any] | None = None,
    evidence_refs: Sequence[str] | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Native Aming-owned issuance entrypoint for an observer session.

    Mints the write-authorizing route token AND returns the consumable handles
    the observer needs to legally enqueue ``execute_backlog_row`` without any
    external-provider dependency at runtime:

    - ``route_token``: the full token (accepted by the mutation gate).
    - ``route_token_ref``: deterministic opaque reference (raw token not in clear).
    - ``merge_queue_id``: deterministic merge-queue id bound to the route identity.
    - ``execute_backlog_row_payload``: ready payload with exactly the required
      route-identity fields.
    - ``provider``: provider evidence (local default or recorded external).

    The raw token text is never persisted in clear by this function; only the
    public token structure and the derived opaque ref are returned.
    """
    token = build_observer_write_route_token(
        project_id=project_id,
        backlog_id=backlog_id,
        task_id=task_id,
        target_files=target_files,
        allowed_actions=allowed_actions,
        ttl_hours=ttl_hours,
        now=now,
        provider=provider,
        evidence_refs=evidence_refs,
        project_root=project_root,
    )
    route_token_ref = derive_route_token_ref(token)
    merge_queue_id = derive_merge_queue_id(token)
    execute_payload = build_execute_backlog_row_payload(
        token,
        route_token_ref=route_token_ref,
        merge_queue_id=merge_queue_id,
    )
    return {
        "schema_version": ISSUE_SCHEMA_VERSION,
        "ok": True,
        "owner": OWNER,
        "caller_role": CALLER_ROLE,
        "external_provider_required": False,
        "judgment_brain_required": False,
        "route_token": token,
        "route_token_ref": route_token_ref,
        "merge_queue_id": merge_queue_id,
        "execute_backlog_row_payload": execute_payload,
        "provider": token.get("provider", {}),
        "route_id": token.get("route_id", ""),
        "route_context_hash": token.get("route_context_hash", ""),
        "prompt_contract_id": token.get("prompt_contract_id", ""),
        "visible_injection_manifest_hash": token.get(
            "visible_injection_manifest_hash", ""
        ),
        "expires_at": token.get("expires_at", ""),
    }
