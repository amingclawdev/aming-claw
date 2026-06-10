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
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

# Import the gate's CANONICAL action normalizer so the mint-time sanitizer and
# the downstream gate (``mf_subagent_contract.validate_route_token_mutation_gate``
# via ``_route_action_allowed`` / ``_normalized_action``) can never drift. The
# gate normalizes BOTH the token's ``allowed_actions`` and the requested action
# (lowercase, ``-``/``.`` -> ``_``, strip) before membership-testing, so a
# crafted variant like ``"Edit-Files"`` or ``"APPLY.PATCH"`` would otherwise pass
# an un-normalized sanitizer at mint and then be ACCEPTED by the gate for the
# canonical blocked action. Importing the gate's own normalizer guarantees the
# sanitizer rejects exactly what the gate would later resolve to. This import has
# no external route-provider side effects (judgment-brain is not pulled in).
from agent.governance.mf_subagent_contract import (
    _normalized_action as _gate_normalized_action,
)


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

    When the caller supplies ``allowed_actions`` they are sanitized at mint: each
    action is first NORMALIZED with the gate's canonical normalizer
    (``mf_subagent_contract._normalized_action``: lowercase, ``-``/``.`` -> ``_``,
    strip) BEFORE the wildcard and blocked-action checks. This is required for
    correctness: the downstream gate normalizes both the stored ``allowed_actions``
    and the requested action before membership-testing and IGNORES
    ``blocked_actions``, so an un-normalized sanitizer would let crafted variants
    (``"Edit-Files"`` -> gate ``"edit_files"``; ``"APPLY.PATCH"`` -> gate
    ``"apply_patch"``; ``"  *  "`` -> gate ``"*"``) slip through at mint and then
    be ACCEPTED by the gate for the canonical wildcard / blocked action. We reject
    (with ``ValueError``) any action whose normalized form is the wildcard ``"*"``
    or intersects the normalized ``BLOCKED_ACTIONS``, and we STORE the normalized
    form in the token so the token's ``allowed_actions`` equal exactly what the
    gate evaluates. This is the choke point that prevents privilege over-reach on
    both the HTTP and MCP issuance paths.
    """
    caller_supplied = allowed_actions is not None
    actions = list(allowed_actions) if allowed_actions else list(DEFAULT_ALLOWED_ACTIONS)
    raw_actions = _string_list(actions)
    # Normalize every action with the SAME logic the gate uses, then dedupe on
    # the normalized form so the stored list matches the gate's evaluated set.
    normalized_actions = _dedupe(
        [_gate_normalized_action(action) for action in raw_actions]
    )
    if not normalized_actions:
        raise ValueError("allowed_actions must be non-empty")

    if caller_supplied:
        if "*" in normalized_actions:
            raise ValueError('allowed_actions must not contain the wildcard "*"')
        blocked_normalized = {_gate_normalized_action(b) for b in BLOCKED_ACTIONS}
        overreach = sorted(set(normalized_actions) & blocked_normalized)
        if overreach:
            raise ValueError(
                "allowed_actions must not include blocked actions: "
                + ", ".join(overreach)
            )
    return normalized_actions


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


# ---------------------------------------------------------------------------
# Route-token ref registry — server-side persistence
# ---------------------------------------------------------------------------
# NEVER store the raw token body.  The registry maps:
#   route_token_ref  ->  salted digest of token body + public identity fields
# so that a caller supplying only the ref can be granted the same gate result
# as a caller supplying the full token, without the secret traveling again.
#
# The salt is derived per-ref so an attacker who learns the digest cannot
# brute-force the token body with a rainbow table over a fixed salt.

REF_REGISTRY_SCHEMA_VERSION = "route_token_ref_registry.v1"
_REF_SALT_LEN = 16  # bytes of per-entry entropy, hex-encoded in DB
_REF_REGISTRY_LOCK = threading.RLock()

# Status values
REF_STATUS_ACTIVE = "active"
REF_STATUS_SUPERSEDED = "superseded"
REF_STATUS_EXPIRED = "expired"


def _token_digest(token: Mapping[str, Any], salt: str) -> str:
    """Return a salted SHA-256 hex digest of the canonical token body.

    The token body is JSON-canonicalized (sorted keys, compact separators) then
    prefixed with the per-entry salt before hashing.  The raw token is NEVER
    stored.  The digest is used only for equality comparison at resolution time.
    """
    canonical = json.dumps(
        dict(token), sort_keys=True, separators=(",", ":"), default=str
    )
    data = f"{salt}:{canonical}".encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _ensure_ref_registry_schema(conn: sqlite3.Connection) -> None:
    """Create the route_token_ref_registry table if it does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observer_route_token_refs (
            project_id          TEXT NOT NULL,
            route_token_ref     TEXT NOT NULL,
            token_digest        TEXT NOT NULL,
            salt                TEXT NOT NULL,
            route_id            TEXT NOT NULL DEFAULT '',
            route_context_hash  TEXT NOT NULL DEFAULT '',
            prompt_contract_id  TEXT NOT NULL DEFAULT '',
            prompt_contract_hash TEXT NOT NULL DEFAULT '',
            visible_injection_manifest_hash TEXT NOT NULL DEFAULT '',
            backlog_id          TEXT NOT NULL DEFAULT '',
            task_id             TEXT NOT NULL DEFAULT '',
            caller_role         TEXT NOT NULL DEFAULT '',
            allowed_actions_json TEXT NOT NULL DEFAULT '[]',
            expires_at          TEXT NOT NULL DEFAULT '',
            evidence_refs_json  TEXT NOT NULL DEFAULT '[]',
            scope_json          TEXT NOT NULL DEFAULT '{}',
            status              TEXT NOT NULL DEFAULT 'active',
            issued_at           TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL,
            PRIMARY KEY (project_id, route_token_ref)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_route_token_refs_status
            ON observer_route_token_refs (project_id, status, route_token_ref)
        """
    )


def persist_route_token_ref(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    route_token_ref: str,
    token: Mapping[str, Any],
) -> None:
    """Persist a minted token into the ref registry.

    Stores only a salted digest of the token body — never the raw token.  If an
    entry for this (project_id, route_token_ref) already exists its digest is
    compared; a mismatch raises ValueError (collision / tampering).  An identical
    re-issue is silently accepted (idempotent).

    Thread-safe: holds ``_REF_REGISTRY_LOCK`` around the upsert.
    """
    project_id = _string(project_id)
    route_token_ref = _string(route_token_ref)
    if not project_id or not route_token_ref:
        raise ValueError("project_id and route_token_ref are required")
    if not isinstance(token, Mapping):
        raise ValueError("token must be a mapping")

    salt = os.urandom(_REF_SALT_LEN).hex()
    digest = _token_digest(token, salt)

    scope = dict(token.get("scope") or {})
    allowed_actions = list(token.get("allowed_actions") or [])
    evidence_refs = list(token.get("evidence_refs") or [])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _ensure_ref_registry_schema(conn)
    with _REF_REGISTRY_LOCK:
        # Check for existing entry
        row = conn.execute(
            "SELECT token_digest, salt, status FROM observer_route_token_refs "
            "WHERE project_id=? AND route_token_ref=?",
            (project_id, route_token_ref),
        ).fetchone()
        if row is not None:
            existing_digest = _token_digest(token, row["salt"])
            if existing_digest != row["token_digest"]:
                raise ValueError(
                    "route_token_ref collision: a different token body is already "
                    f"registered under ref {route_token_ref!r}"
                )
            # Idempotent re-issue: same token, already registered — nothing to do.
            return

        conn.execute(
            """
            INSERT INTO observer_route_token_refs
                (project_id, route_token_ref, token_digest, salt,
                 route_id, route_context_hash, prompt_contract_id,
                 prompt_contract_hash, visible_injection_manifest_hash,
                 backlog_id, task_id, caller_role,
                 allowed_actions_json, expires_at, evidence_refs_json,
                 scope_json, status, issued_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                project_id,
                route_token_ref,
                digest,
                salt,
                _string(token.get("route_id")),
                _string(token.get("route_context_hash")),
                _string(token.get("prompt_contract_id")),
                _string(token.get("prompt_contract_hash")),
                _string(token.get("visible_injection_manifest_hash")),
                _string(scope.get("backlog_id")),
                _string(scope.get("task_id")),
                _string(token.get("caller_role")),
                json.dumps(allowed_actions),
                _string(token.get("expires_at")),
                json.dumps(evidence_refs),
                json.dumps(scope),
                REF_STATUS_ACTIVE,
                _string(token.get("issued_at")) or now_str,
                now_str,
            ),
        )
        conn.commit()


def resolve_route_token_ref(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    route_token_ref: str,
    route_id: str = "",
    route_context_hash: str = "",
    task_id: str = "",
    backlog_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Resolve a route_token_ref to its stored public identity.

    Returns a dict of public token fields sufficient for the gate (route identity,
    allowed_actions, scope, expiry, evidence_refs) **without** the raw token body.
    Returns ``None`` when the ref is unknown.

    Fails closed:
    - Unknown ref → ``None`` (caller must treat as gate failure).
    - ``status != 'active'`` (superseded / expired) → raises
      ``RouteTokenRefError`` with the status.
    - Identity mismatch (route_id / route_context_hash / task_id / backlog_id
      when supplied) → raises ``RouteTokenRefError``.
    - Expired (``expires_at`` in the past) → raises ``RouteTokenRefError``.
    """
    project_id = _string(project_id)
    route_token_ref = _string(route_token_ref)
    if not project_id or not route_token_ref:
        return None

    try:
        _ensure_ref_registry_schema(conn)
        row = conn.execute(
            "SELECT * FROM observer_route_token_refs "
            "WHERE project_id=? AND route_token_ref=?",
            (project_id, route_token_ref),
        ).fetchone()
    except sqlite3.OperationalError:
        return None

    if row is None:
        return None

    row_dict = dict(row)
    status = _string(row_dict.get("status"))
    if status != REF_STATUS_ACTIVE:
        raise RouteTokenRefError(
            f"route_token_ref {route_token_ref!r} is not active (status={status!r}); "
            "ref resolution refused"
        )

    # Identity binding checks — only when the caller supplies non-empty values.
    if route_id:
        stored_route_id = _string(row_dict.get("route_id"))
        if stored_route_id and stored_route_id != _string(route_id):
            raise RouteTokenRefError(
                f"route_token_ref identity mismatch: route_id {route_id!r} does not "
                f"match registered {stored_route_id!r}"
            )
    if route_context_hash:
        stored_rch = _string(row_dict.get("route_context_hash"))
        if stored_rch and stored_rch != _string(route_context_hash):
            raise RouteTokenRefError(
                "route_token_ref identity mismatch: route_context_hash does not match"
            )
    if task_id:
        stored_task = _string(row_dict.get("task_id"))
        if stored_task and stored_task != _string(task_id):
            raise RouteTokenRefError(
                f"route_token_ref identity mismatch: task_id {task_id!r} does not "
                f"match registered {stored_task!r}"
            )
    if backlog_id:
        stored_bl = _string(row_dict.get("backlog_id"))
        if stored_bl and stored_bl != _string(backlog_id):
            raise RouteTokenRefError(
                f"route_token_ref identity mismatch: backlog_id {backlog_id!r} does not "
                f"match registered {stored_bl!r}"
            )

    # Expiry check
    expires_at = _string(row_dict.get("expires_at"))
    if expires_at:
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        try:
            expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if now_dt >= expires_dt:
                raise RouteTokenRefError(
                    f"route_token_ref {route_token_ref!r} has expired (expires_at={expires_at!r})"
                )
        except (ValueError, TypeError):
            pass  # Unparseable expiry: do not block resolution

    # Reconstruct a public-only token surface (sufficient for the gate)
    try:
        allowed_actions = json.loads(row_dict.get("allowed_actions_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        allowed_actions = []
    try:
        evidence_refs = json.loads(row_dict.get("evidence_refs_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        evidence_refs = []
    try:
        scope = json.loads(row_dict.get("scope_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        scope = {}

    return {
        "schema_version": REF_REGISTRY_SCHEMA_VERSION,
        "route_token_ref": route_token_ref,
        "route_id": _string(row_dict.get("route_id")),
        "route_context_hash": _string(row_dict.get("route_context_hash")),
        "prompt_contract_id": _string(row_dict.get("prompt_contract_id")),
        "prompt_contract_hash": _string(row_dict.get("prompt_contract_hash")),
        "visible_injection_manifest_hash": _string(
            row_dict.get("visible_injection_manifest_hash")
        ),
        "caller_role": _string(row_dict.get("caller_role")),
        "allowed_actions": allowed_actions,
        "evidence_refs": evidence_refs,
        "expires_at": expires_at,
        "scope": scope,
        "status": status,
        "resolved_from_ref": True,
    }


def supersede_route_token_ref(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    route_token_ref: str,
) -> bool:
    """Mark a route_token_ref as superseded.

    Called by the lifecycle wire (F5) when a ``route.identity.superseded``
    (``route_identity_supersede``) event is recorded during repair-run
    route-evidence processing.  After supersession,
    ``resolve_route_token_ref`` will raise ``RouteTokenRefError`` for this
    ref, so any stale-identity ref presented afterward is refused (fail
    closed).

    Returns True if the ref was found and updated, False if not found (e.g.
    ref was already superseded, never persisted, or belongs to a different
    project).
    """
    project_id = _string(project_id)
    route_token_ref = _string(route_token_ref)
    if not project_id or not route_token_ref:
        return False
    try:
        _ensure_ref_registry_schema(conn)
        cursor = conn.execute(
            "UPDATE observer_route_token_refs SET status=? "
            "WHERE project_id=? AND route_token_ref=? AND status=?",
            (REF_STATUS_SUPERSEDED, project_id, route_token_ref, REF_STATUS_ACTIVE),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        return False


class RouteTokenRefError(Exception):
    """Raised by resolve_route_token_ref for non-active / mismatched refs."""
