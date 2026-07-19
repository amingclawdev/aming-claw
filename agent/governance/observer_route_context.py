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

Provider independence: AC 不依赖 JB(fail-open),但可 best-effort 消费其 advisory
hints. ``judgment_brain_required=False`` means judgment-brain is not required
for route minting; it does not mean advisory data is never consumed. This module
still does NOT import or call any external route provider at import time or
runtime. ``resolve_route_provider`` only *records* whether an external provider
is declared in project config; when one is declared the caller is told to route
issuance through it, but this module never invokes it. When none is declared,
the aming-local default (``owner="aming-claw"``) is used.

Consumability: ``issue_observer_write_route_context`` returns, in addition to the
token, a deterministic opaque ``route_token_ref`` (the raw token text is never
embedded in clear in the ref) and a ``merge_queue_id``, plus a ready
``execute_backlog_row_payload`` carrying exactly the fields the observer
``execute_backlog_row`` command requires.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hmac
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

ROUTE_ACTION_SCOPE_SCHEMA_VERSION = "observer_route_action_scope.v2"

# These observer-owned actions are governance/evidence/reconcile/close-control
# operations. A route token scoped only to these actions must be copy-safe for
# observer work and must not advertise that a fresh mf_sub implementation lane
# exists or is legally required.
OBSERVER_ADMIN_CLOSE_EVIDENCE_ACTIONS: tuple[str, ...] = (
    "backlog_upsert",
    "backlog_close",
    "task_timeline_append",
    "graph_query",
    "graph_current_full_reconcile",
    "contract_runtime_current",
    "contract_runtime_guide",
    "contract_runtime_recover",
    "contract_runtime_submit_line",
    "contract_runtime_bypass_line",
    "observer_direct_mutation_exception",
    "runtime_context_read_receipt",
    "submit_mf_subagent_read_receipt",
    "record_mf_subagent_startup",
    "record_implementation_evidence",
    "runtime_context_implementation_evidence",
    "record_finish_time_worker_attestation",
    "runtime_context_finish_time_worker_attestation",
    "record_finish_gate",
    "runtime_context_finish_gate",
    "observer_route_context_current",
    "observer_route_context_renew",
    "renew_route_token_ref",
    "route_token_ref_renewal_next_action",
    "resolve_route_token_ref",
    "capture_intent",
    "run_route_precheck",
    "verify_evidence",
)

# Any action in this set either dispatches implementation work, enters a worker
# topology, or performs merge/close materialization. Those paths keep the
# bounded worker and independent verification lane guidance intact.
IMPLEMENTATION_OR_MERGE_ACTIONS: tuple[str, ...] = (
    "parallel_branch_allocate",
    "dispatch_bounded_lane",
    "execute_backlog_row",
    "close_or_merge_after_evidence",
    "direct_fix_enter",
    "mf_parallel_enter",
    "mf_batch_parallel_enter",
    "mf_parallel_merge",
    "mf_batch_merge",
    "parallel_branch_merge_queue_materialize",
    "parallel_branch_merge_queue_apply",
    "merge_queue",
    "merge_execute",
    "merge_result",
    "merge",
)

# These actions are local steps in an explicitly selected, operator-supervised
# direct-main round.  They are *not* observer-admin actions globally: merge must
# continue to require worker/merge lanes unless the same route scope also carries
# ``observer_direct_mutation_exception``.
OBSERVER_DIRECT_MAIN_LOCAL_ROUND_ACTIONS: tuple[str, ...] = (
    "run_tests",
    "git_diff",
    "merge",
)

OBSERVER_DIRECT_MAIN_REQUIRED_LANES: tuple[dict[str, str], ...] = (
    {
        "id": "observer_direct_main_action",
        "role": "observer",
        "purpose": (
            "perform scoped non-implementation governance, evidence, or graph "
            "reconcile work without implying an mf_sub implementation lane"
        ),
    },
)

OBSERVER_DIRECT_MAIN_REQUIRED_EVIDENCE: tuple[str, ...] = (
    "route_context_hash",
    "prompt_contract_id",
    "visible_injection_manifest_or_hash",
    "caller_role",
    "allowed_actions",
    "blocked_actions",
    "route_token_ref",
    "observer_session_id_or_route_owned_source_event",
)

PARENT_ROUTE_LINEAGE_SCHEMA_VERSION = "parent_route_lineage.v1"
CHILD_ROUTE_LINEAGE_SCHEMA_VERSION = "child_route_lineage.v1"
ROUTE_LINEAGE_SCHEMA_VERSION = "observer_route_parent_child_lineage.v1"

_PARENT_ROUTE_REQUIRED_FIELDS: tuple[str, ...] = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "visible_injection_manifest_hash",
)
_PARENT_ROUTE_OPTIONAL_FIELDS: tuple[str, ...] = (
    "route_token_ref",
    "selected_project",
    "selected_backlog_id",
)
_PARENT_ROUTE_RAW_TOKEN_FIELDS: tuple[str, ...] = (
    "route_token",
    "raw_route_token",
    "token",
    "token_body",
    "session_token",
)
_PARENT_ROUTE_CANONICAL_EVENT_IDS: tuple[str, ...] = (
    "event.route_prompt_context.preview",
    "event.route_action.pre_mutation",
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


def _route_lane_requirements_for_actions(
    allowed_actions: Sequence[str],
) -> dict[str, Any]:
    """Return copy-safe route guidance for the token's allowed action scope."""

    actions = _normalized_action_list(allowed_actions)
    action_set = set(actions)
    admin_close_evidence_actions = {
        _gate_normalized_action(action)
        for action in OBSERVER_ADMIN_CLOSE_EVIDENCE_ACTIONS
    }
    implementation_or_merge_actions = {
        _gate_normalized_action(action)
        for action in IMPLEMENTATION_OR_MERGE_ACTIONS
    }
    direct_main_round = "observer_direct_mutation_exception" in action_set
    direct_main_local_round_actions = {
        _gate_normalized_action(action)
        for action in OBSERVER_DIRECT_MAIN_LOCAL_ROUND_ACTIONS
    }
    direct_main_local_markers = (
        sorted(action_set & direct_main_local_round_actions)
        if direct_main_round
        else []
    )
    unknown_action_set = (
        action_set - admin_close_evidence_actions - implementation_or_merge_actions
    )
    implementation_marker_set = action_set & implementation_or_merge_actions
    if direct_main_round:
        unknown_action_set -= direct_main_local_round_actions
        # ``merge`` is direct-main-local only when the explicit direct-main
        # mutation-exception marker is present in the same scoped route.
        implementation_marker_set -= direct_main_local_round_actions
    unknown_actions = sorted(unknown_action_set)
    implementation_markers = sorted(implementation_marker_set)
    admin_close_evidence_markers = sorted(
        action_set & admin_close_evidence_actions
    )
    requires_worker_lane = bool(implementation_markers or unknown_actions)

    if requires_worker_lane:
        classification = "bounded_worker_implementation_or_merge"
        required_lanes = [dict(lane) for lane in REQUIRED_LANES]
        required_evidence = list(REQUIRED_EVIDENCE)
        copy_safe_guidance = (
            "This route action scope can dispatch implementation or merge/close "
            "work; bounded mf_sub implementation and independent verification "
            "lane requirements remain in force."
        )
    elif direct_main_round:
        classification = "observer_direct_main_full_round"
        required_lanes = [dict(lane) for lane in OBSERVER_DIRECT_MAIN_REQUIRED_LANES]
        required_evidence = list(OBSERVER_DIRECT_MAIN_REQUIRED_EVIDENCE)
        copy_safe_guidance = (
            "This route action scope explicitly selects the operator-supervised "
            "direct-main round; its bounded local test, diff, reconcile, merge, "
            "and close steps do not imply a fresh mf_sub implementation lane."
        )
    else:
        classification = "observer_admin_close_evidence_only"
        required_lanes = [dict(lane) for lane in OBSERVER_DIRECT_MAIN_REQUIRED_LANES]
        required_evidence = list(OBSERVER_DIRECT_MAIN_REQUIRED_EVIDENCE)
        copy_safe_guidance = (
            "This route action scope is limited to observer-owned admin, "
            "close-control, evidence, contract-runtime, route-token, or graph "
            "reconcile work and does not require or imply a fresh mf_sub "
            "implementation lane."
        )

    return {
        "required_lanes": required_lanes,
        "required_evidence": required_evidence,
        "route_action_scope": {
            "schema_version": ROUTE_ACTION_SCOPE_SCHEMA_VERSION,
            "classification": classification,
            "allowed_actions": actions,
            "direct_main_or_reconcile_actions": admin_close_evidence_markers,
            "admin_close_or_evidence_actions": admin_close_evidence_markers,
            "implementation_or_merge_actions": implementation_markers,
            "direct_main_full_round": direct_main_round,
            "direct_main_local_actions": direct_main_local_markers,
            "unknown_actions": unknown_actions,
            "requires_mf_sub_implementation_lane": requires_worker_lane,
            "requires_bounded_worker_implementation": requires_worker_lane,
            "copy_safe": True,
            "copy_safe_guidance": copy_safe_guidance,
        },
    }


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
    parent_route_identity: Mapping[str, Any] | None = None,
    parent_route_id: str = "",
    parent_route_context_hash: str = "",
    parent_prompt_contract_id: str = "",
    parent_prompt_contract_hash: str = "",
    parent_visible_injection_manifest_hash: str = "",
    parent_route_token_ref: str = "",
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
    lane_requirements = _route_lane_requirements_for_actions(actions_list)
    required_lanes = lane_requirements["required_lanes"]
    required_evidence = lane_requirements["required_evidence"]
    route_action_scope = lane_requirements["route_action_scope"]

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
    parent_lineage = build_parent_route_lineage(
        parent_route_identity,
        project_id=project_id,
        backlog_id=backlog_id,
        parent_route_id=parent_route_id,
        parent_route_context_hash=parent_route_context_hash,
        parent_prompt_contract_id=parent_prompt_contract_id,
        parent_prompt_contract_hash=parent_prompt_contract_hash,
        parent_visible_injection_manifest_hash=parent_visible_injection_manifest_hash,
        parent_route_token_ref=parent_route_token_ref,
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
        "required_lanes": required_lanes,
        "required_evidence": required_evidence,
        "route_action_scope": route_action_scope,
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
        "required_lanes": required_lanes,
        "required_evidence": required_evidence,
        "route_action_scope": route_action_scope,
        "requires_mf_sub_implementation_lane": route_action_scope[
            "requires_mf_sub_implementation_lane"
        ],
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
    if parent_lineage:
        token["parent_route_lineage"] = parent_lineage
    return token


def derive_route_token_ref(token: Mapping[str, Any]) -> str:
    """Derive a deterministic, opaque, consumable reference for a minted token.

    The reference is a stable handle the observer can pass to downstream
    consumers (e.g. an ``execute_backlog_row`` command payload). It is derived
    from the token's PUBLIC route identity and public issue/expiry timestamps —
    it never embeds the raw token body in clear, so persisting the ref leaks no
    secret. Two mints of the same identity at the same timestamp yield the same
    ref; same-identity reissues at a later timestamp yield a fresh ref so the
    new full token remains digest-bindable.
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
        "issued_at": _string(token.get("issued_at")),
        "expires_at": _string(token.get("expires_at") or token.get("expiry")),
        "scope": dict(token.get("scope") or {}),
    }
    parent_lineage = token.get("parent_route_lineage")
    if isinstance(parent_lineage, Mapping):
        identity["parent_route_lineage_hash"] = _sha256(
            {
                field: _string(parent_lineage.get(field))
                for field in (
                    *_PARENT_ROUTE_REQUIRED_FIELDS,
                    *_PARENT_ROUTE_OPTIONAL_FIELDS,
                )
            }
        )
    if not identity["route_context_hash"] or not identity["prompt_contract_id"]:
        raise ValueError("token is missing route identity required to derive a ref")
    return "rtok-" + _stable_digest(identity, length=32)


def _normalized_action_list(value: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in _string_list(value):
        normalized = _gate_normalized_action(item)
        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out


def _first_string(source: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = _string(source.get(name))
        if value:
            return value
    return ""


def _merge_parent_route_field(
    parent: dict[str, Any],
    *,
    field: str,
    value: Any,
) -> None:
    token = _string(value)
    if not token:
        return
    existing = _string(parent.get(field))
    if existing and existing != token:
        raise ValueError(
            f"parent_route_identity mismatch for {field}: "
            f"{existing!r} != {token!r}"
        )
    parent[field] = token


def _validate_hash_field(lineage: Mapping[str, Any], field: str) -> None:
    value = _string(lineage.get(field))
    if not value.startswith("sha256:"):
        raise ValueError(f"parent_route_identity {field} must be a sha256: hash")


def _valid_parent_route_id(route_id: str) -> bool:
    route_id = _string(route_id)
    return route_id.startswith("route-") or route_id in _PARENT_ROUTE_CANONICAL_EVENT_IDS


def _parent_route_identity_from_inputs(
    parent_route_identity: Mapping[str, Any] | None,
    *,
    parent_route_id: str = "",
    parent_route_context_hash: str = "",
    parent_prompt_contract_id: str = "",
    parent_prompt_contract_hash: str = "",
    parent_visible_injection_manifest_hash: str = "",
    parent_route_token_ref: str = "",
) -> tuple[bool, dict[str, Any]]:
    supplied = parent_route_identity is not None or any(
        _string(value)
        for value in (
            parent_route_id,
            parent_route_context_hash,
            parent_prompt_contract_id,
            parent_prompt_contract_hash,
            parent_visible_injection_manifest_hash,
            parent_route_token_ref,
        )
    )
    if not supplied:
        return False, {}
    if parent_route_identity is not None and not isinstance(parent_route_identity, Mapping):
        raise ValueError("parent_route_identity must be a mapping")

    parent = dict(parent_route_identity or {})
    for raw_key in _PARENT_ROUTE_RAW_TOKEN_FIELDS:
        if _string(parent.get(raw_key)):
            raise ValueError(
                "parent_route_identity must not include raw route/session token bodies; "
                "pass only route_token_ref when an opaque ref is available"
            )

    aliases = {
        "route_id": _first_string(parent, "route_id", "parent_route_id"),
        "route_context_hash": _first_string(
            parent, "route_context_hash", "parent_route_context_hash"
        ),
        "prompt_contract_id": _first_string(
            parent, "prompt_contract_id", "parent_prompt_contract_id"
        ),
        "prompt_contract_hash": _first_string(
            parent, "prompt_contract_hash", "parent_prompt_contract_hash"
        ),
        "visible_injection_manifest_hash": _first_string(
            parent,
            "visible_injection_manifest_hash",
            "parent_visible_injection_manifest_hash",
        ),
        "route_token_ref": _first_string(
            parent, "route_token_ref", "parent_route_token_ref"
        ),
        "selected_project": _first_string(
            parent, "selected_project", "project_id", "target_project_id"
        ),
        "selected_backlog_id": _first_string(
            parent, "selected_backlog_id", "backlog_id", "bug_id"
        ),
    }
    normalized: dict[str, Any] = {}
    for field, value in aliases.items():
        _merge_parent_route_field(normalized, field=field, value=value)
    for field, value in (
        ("route_id", parent_route_id),
        ("route_context_hash", parent_route_context_hash),
        ("prompt_contract_id", parent_prompt_contract_id),
        ("prompt_contract_hash", parent_prompt_contract_hash),
        ("visible_injection_manifest_hash", parent_visible_injection_manifest_hash),
        ("route_token_ref", parent_route_token_ref),
    ):
        _merge_parent_route_field(normalized, field=field, value=value)
    return True, normalized


def build_parent_route_lineage(
    parent_route_identity: Mapping[str, Any] | None,
    *,
    project_id: str,
    backlog_id: str,
    parent_route_id: str = "",
    parent_route_context_hash: str = "",
    parent_prompt_contract_id: str = "",
    parent_prompt_contract_hash: str = "",
    parent_visible_injection_manifest_hash: str = "",
    parent_route_token_ref: str = "",
) -> dict[str, Any] | None:
    """Normalize a complete public parent route identity into lineage evidence.

    The parent binding is intentionally fail-closed: once the caller supplies
    any parent identity field, the public canonical identity must be complete
    and must not contradict the child issue scope. Raw token bodies are refused;
    only opaque ``rtok-`` references may be carried.
    """

    supplied, parent = _parent_route_identity_from_inputs(
        parent_route_identity,
        parent_route_id=parent_route_id,
        parent_route_context_hash=parent_route_context_hash,
        parent_prompt_contract_id=parent_prompt_contract_id,
        parent_prompt_contract_hash=parent_prompt_contract_hash,
        parent_visible_injection_manifest_hash=parent_visible_injection_manifest_hash,
        parent_route_token_ref=parent_route_token_ref,
    )
    if not supplied:
        return None

    missing = [
        field for field in _PARENT_ROUTE_REQUIRED_FIELDS if not _string(parent.get(field))
    ]
    if missing:
        raise ValueError(
            "parent_route_identity incomplete; missing required fields: "
            + ", ".join(missing)
        )
    if not _valid_parent_route_id(_string(parent.get("route_id"))):
        raise ValueError(
            "parent_route_identity route_id must be a public canonical route id "
            "(route-*, event.route_prompt_context.preview, or "
            "event.route_action.pre_mutation)"
        )
    if not _string(parent.get("prompt_contract_id")).startswith("rprompt-"):
        raise ValueError(
            "parent_route_identity prompt_contract_id must be a canonical rprompt-* id"
        )
    for field in (
        "route_context_hash",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        _validate_hash_field(parent, field)

    selected_project = _string(parent.get("selected_project"))
    if selected_project and selected_project != project_id:
        raise ValueError(
            "parent_route_identity project mismatch: "
            f"{selected_project!r} != {project_id!r}"
        )
    selected_backlog = _string(parent.get("selected_backlog_id"))
    if selected_backlog and selected_backlog != backlog_id:
        raise ValueError(
            "parent_route_identity backlog mismatch: "
            f"{selected_backlog!r} != {backlog_id!r}"
        )
    route_token_ref = _string(parent.get("route_token_ref"))
    if route_token_ref and not route_token_ref.startswith("rtok-"):
        raise ValueError("parent_route_token_ref must be an opaque rtok-* reference")

    lineage = {
        "schema_version": PARENT_ROUTE_LINEAGE_SCHEMA_VERSION,
        "route_id": _string(parent.get("route_id")),
        "route_context_hash": _string(parent.get("route_context_hash")),
        "prompt_contract_id": _string(parent.get("prompt_contract_id")),
        "prompt_contract_hash": _string(parent.get("prompt_contract_hash")),
        "visible_injection_manifest_hash": _string(
            parent.get("visible_injection_manifest_hash")
        ),
        "selected_project": selected_project or project_id,
        "selected_backlog_id": selected_backlog or backlog_id,
        "binding_status": "parent_bound",
        "binding_source": "parent_route_identity",
    }
    if route_token_ref:
        lineage["route_token_ref"] = route_token_ref
    return lineage


def _child_route_lineage(
    token: Mapping[str, Any],
    *,
    route_token_ref: str = "",
    merge_queue_id: str = "",
) -> dict[str, Any]:
    scope = dict(token.get("scope") or {})
    lineage = {
        "schema_version": CHILD_ROUTE_LINEAGE_SCHEMA_VERSION,
        "route_id": _string(token.get("route_id")),
        "route_context_hash": _string(token.get("route_context_hash")),
        "prompt_contract_id": _string(token.get("prompt_contract_id")),
        "prompt_contract_hash": _string(token.get("prompt_contract_hash")),
        "visible_injection_manifest_hash": _string(
            token.get("visible_injection_manifest_hash")
        ),
        "project_id": _string(scope.get("project_id")),
        "backlog_id": _string(scope.get("backlog_id")),
        "task_id": _string(scope.get("task_id")),
        "caller_role": _string(token.get("caller_role")),
        "allowed_actions": list(token.get("allowed_actions") or []),
        "blocked_actions": list(token.get("blocked_actions") or []),
    }
    if route_token_ref:
        lineage["route_token_ref"] = _string(route_token_ref)
    if merge_queue_id:
        lineage["merge_queue_id"] = _string(merge_queue_id)
    return lineage


def _attach_route_lineage(
    token: dict[str, Any],
    *,
    route_token_ref: str = "",
    merge_queue_id: str = "",
) -> None:
    parent = token.get("parent_route_lineage")
    if not isinstance(parent, Mapping):
        return
    child = _child_route_lineage(
        token,
        route_token_ref=route_token_ref,
        merge_queue_id=merge_queue_id,
    )
    token["child_route_lineage"] = child
    token["route_lineage"] = {
        "schema_version": ROUTE_LINEAGE_SCHEMA_VERSION,
        "status": "parent_bound",
        "parent_route_id": _string(parent.get("route_id")),
        "parent_route_context_hash": _string(parent.get("route_context_hash")),
        "parent_prompt_contract_id": _string(parent.get("prompt_contract_id")),
        "child_route_id": child["route_id"],
        "child_route_context_hash": child["route_context_hash"],
        "child_prompt_contract_id": child["prompt_contract_id"],
        "parent_route_lineage": dict(parent),
        "child_route_lineage": child,
        "raw_route_token_persisted": False,
        "raw_session_token_persisted": False,
    }


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
    payload = {
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
    for key in ("parent_route_lineage", "child_route_lineage", "route_lineage"):
        value = token.get(key)
        if isinstance(value, Mapping):
            payload[key] = dict(value)
    return payload


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
    parent_route_identity: Mapping[str, Any] | None = None,
    parent_route_id: str = "",
    parent_route_context_hash: str = "",
    parent_prompt_contract_id: str = "",
    parent_prompt_contract_hash: str = "",
    parent_visible_injection_manifest_hash: str = "",
    parent_route_token_ref: str = "",
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
        parent_route_identity=parent_route_identity,
        parent_route_id=parent_route_id,
        parent_route_context_hash=parent_route_context_hash,
        parent_prompt_contract_id=parent_prompt_contract_id,
        parent_prompt_contract_hash=parent_prompt_contract_hash,
        parent_visible_injection_manifest_hash=parent_visible_injection_manifest_hash,
        parent_route_token_ref=parent_route_token_ref,
    )
    route_token_ref = derive_route_token_ref(token)
    merge_queue_id = derive_merge_queue_id(token)
    _attach_route_lineage(
        token,
        route_token_ref=route_token_ref,
        merge_queue_id=merge_queue_id,
    )
    execute_payload = build_execute_backlog_row_payload(
        token,
        route_token_ref=route_token_ref,
        merge_queue_id=merge_queue_id,
    )
    result = {
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
        "route_action_scope": dict(token.get("route_action_scope") or {}),
        "requires_mf_sub_implementation_lane": bool(
            token.get("requires_mf_sub_implementation_lane")
        ),
        "expires_at": token.get("expires_at", ""),
    }
    for key in ("parent_route_lineage", "child_route_lineage", "route_lineage"):
        value = token.get(key)
        if isinstance(value, Mapping):
            result[key] = dict(value)
    return result


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
REF_RENEWAL_SCHEMA_VERSION = "route_token_ref_renewal.v1"
REF_RENEWAL_PROOF_SCHEMA_VERSION = "route_token_ref_renewal_proof.v1"
REF_EXPIRY_STATUS_SCHEMA_VERSION = "route_token_ref_expiry_status.v1"
REF_RENEWAL_NEXT_ACTION_SCHEMA_VERSION = "route_token_ref_renewal_next_action.v1"
REF_REISSUE_NEXT_ACTION_SCHEMA_VERSION = "route_token_ref_same_scope_issue_next_action.v1"
_REF_SALT_LEN = 16  # bytes of per-entry entropy, hex-encoded in DB
_REF_REGISTRY_LOCK = threading.RLock()
ROUTE_TOKEN_REF_RENEW_WITHIN_SECONDS = 15 * 60

# Status values
REF_STATUS_ACTIVE = "active"
REF_STATUS_SUPERSEDED = "superseded"
REF_STATUS_EXPIRED = "expired"
_REF_LINEAGE_COLUMNS = {
    "parent_route_lineage": "parent_route_lineage_json",
    "child_route_lineage": "child_route_lineage_json",
    "route_lineage": "route_lineage_json",
}
_REF_SCOPE_LIST_COLUMNS = {
    "target_files": "target_files_json",
    "owned_files": "owned_files_json",
}


def _public_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_dumps_public_mapping(value: Any) -> str:
    payload = _public_mapping(value)
    if not payload:
        return "{}"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _json_loads_public_mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_dumps_string_list(value: Any) -> str:
    return json.dumps(_dedupe(_string_list(value)), sort_keys=True)


def _json_loads_string_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        parsed = []
    return _dedupe(_string_list(parsed))


def _json_loads_mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _utc_datetime(value: datetime | None = None) -> datetime:
    dt = value or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_utc_datetime(value: Any) -> datetime | None:
    text = _string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return _utc_datetime(parsed)


def route_token_ref_renewal_next_action(
    *,
    project_id: str = "",
    backlog_id: str = "",
    task_id: str = "",
    route_token_ref: str = "",
    observer_session_id: str = "",
    reason: str = "",
    allowed_actions: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    owned_files: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    parent_route_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the public-safe semantic action for renewing a route-token ref."""

    reason_text = _string(reason)
    if reason_text == REF_STATUS_SUPERSEDED:
        return route_token_ref_same_scope_issue_next_action(
            project_id=project_id,
            backlog_id=backlog_id,
            task_id=task_id,
            route_token_ref=route_token_ref,
            observer_session_id=observer_session_id,
            reason=reason_text,
            allowed_actions=allowed_actions,
            target_files=target_files,
            owned_files=owned_files,
            evidence_refs=evidence_refs,
            parent_route_identity=parent_route_identity,
        )

    return {
        "schema_version": REF_RENEWAL_NEXT_ACTION_SCHEMA_VERSION,
        "action": "renew_route_token_ref",
        "semantic_next_action": "observer_route_context_renew",
        "mcp_tool": "observer_route_context_renew",
        "http_entrypoint": {
            "method": "POST",
            "path": "/api/projects/{project_id}/observer/route-context/renew",
            "path_params": {"project_id": _string(project_id) or "{project_id}"},
        },
        "required_fields": [
            "project_id",
            "observer_session_id",
            "route_token_ref",
            "backlog_id",
            "task_id",
        ],
        "project_id": _string(project_id),
        "observer_session_id": _string(observer_session_id),
        "route_token_ref": _string(route_token_ref),
        "scope": {
            "project_id": _string(project_id),
            "backlog_id": _string(backlog_id),
            "task_id": _string(task_id),
        },
        "reason": reason_text,
        "observer_session_must_be_active": True,
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
    }


def route_token_ref_same_scope_issue_next_action(
    *,
    project_id: str = "",
    backlog_id: str = "",
    task_id: str = "",
    route_token_ref: str = "",
    observer_session_id: str = "",
    reason: str = "",
    allowed_actions: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    owned_files: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    parent_route_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return guidance for replacing a superseded route-token ref without renew loops."""

    project = _string(project_id)
    backlog = _string(backlog_id)
    task = _string(task_id)
    parent_identity = _public_mapping(parent_route_identity)
    actions = _normalized_action_list(allowed_actions or [])
    targets = _string_list(target_files or [])
    owned = _string_list(owned_files or [])
    evidence = _dedupe(
        [
            *_string_list(evidence_refs or []),
            f"reissued_from:{_string(route_token_ref)}" if _string(route_token_ref) else "",
        ]
    )
    issue_payload: dict[str, Any] = {
        "project_id": project,
        "caller_role": CALLER_ROLE,
        "backlog_id": backlog,
        "task_id": task,
    }
    if actions:
        issue_payload["allowed_actions"] = actions
    if targets:
        issue_payload["target_files"] = targets
    if owned:
        issue_payload["owned_files"] = owned
    if evidence:
        issue_payload["evidence_refs"] = evidence
    if parent_identity:
        issue_payload["parent_route_identity"] = dict(parent_identity)
        parent_ref = _string(
            parent_identity.get("route_token_ref")
            or parent_identity.get("parent_route_token_ref")
        )
        if parent_ref:
            issue_payload["parent_route_token_ref"] = parent_ref

    required_fields = [
        "project_id",
        "observer_session_id",
        "backlog_id",
        "task_id",
        "allowed_actions",
    ]
    if parent_identity:
        required_fields.append("parent_route_identity")

    action = {
        "schema_version": REF_REISSUE_NEXT_ACTION_SCHEMA_VERSION,
        "action": "issue_fresh_same_scope_route_token_ref",
        "semantic_next_action": "observer_route_context_issue",
        "mcp_tool": "observer_route_context_issue",
        "http_entrypoint": {
            "method": "POST",
            "path": "/api/projects/{project_id}/observer/route-context/issue",
            "path_params": {"project_id": project or "{project_id}"},
        },
        "required_fields": required_fields,
        "project_id": project,
        "observer_session_id": _string(observer_session_id),
        "superseded_route_token_ref": _string(route_token_ref),
        "scope": {
            "project_id": project,
            "backlog_id": backlog,
            "task_id": task,
        },
        "reason": _string(reason) or REF_STATUS_SUPERSEDED,
        "preferred_over": "renew_route_token_ref",
        "renew_loop_allowed": False,
        "same_scope_required": True,
        "expired_or_near_expired_refs_still_use_renew": True,
        "observer_route_context_issue_payload": issue_payload,
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
    }
    if parent_identity:
        action["parent_route_identity_required"] = True
        action["parent_route_identity"] = dict(parent_identity)
    return action


def route_token_ref_expiry_status(
    expires_at: Any,
    *,
    now: datetime | None = None,
    renew_within_seconds: int = ROUTE_TOKEN_REF_RENEW_WITHIN_SECONDS,
    project_id: str = "",
    backlog_id: str = "",
    task_id: str = "",
    route_token_ref: str = "",
    observer_session_id: str = "",
) -> dict[str, Any]:
    """Classify expiry and attach renewal guidance when a ref should rotate."""

    now_dt = _utc_datetime(now)
    expires_text = _string(expires_at)
    renew_window = max(0, int(renew_within_seconds or 0))
    parsed = _parse_utc_datetime(expires_text)
    status = "missing_expiry" if not expires_text else "invalid_expiry"
    seconds_remaining: int | None = None
    expired = False
    near_expiry = False
    if parsed is not None:
        seconds_remaining = int((parsed - now_dt).total_seconds())
        expired = seconds_remaining <= 0
        near_expiry = not expired and seconds_remaining <= renew_window
        status = "expired" if expired else "near_expiry" if near_expiry else "valid"
    payload: dict[str, Any] = {
        "schema_version": REF_EXPIRY_STATUS_SCHEMA_VERSION,
        "status": status,
        "expires_at": expires_text,
        "seconds_remaining": seconds_remaining,
        "expired": expired,
        "near_expiry": near_expiry,
        "renewal_recommended": expired or near_expiry,
        "renew_within_seconds": renew_window,
    }
    if expired or near_expiry:
        payload["next_action"] = route_token_ref_renewal_next_action(
            project_id=project_id,
            backlog_id=backlog_id,
            task_id=task_id,
            route_token_ref=route_token_ref,
            observer_session_id=observer_session_id,
            reason=status,
        )
    return payload


def _lineage_conflicting_fields(
    existing: Mapping[str, Any],
    supplied: Mapping[str, Any],
    *,
    public_key: str,
) -> list[str]:
    if public_key == "route_lineage":
        fields = (
            "parent_route_id",
            "parent_route_context_hash",
            "parent_prompt_contract_id",
            "child_route_id",
            "child_route_context_hash",
            "child_prompt_contract_id",
        )
    else:
        fields = (
            "route_id",
            "route_context_hash",
            "prompt_contract_id",
            "prompt_contract_hash",
            "visible_injection_manifest_hash",
            "project_id",
            "backlog_id",
            "task_id",
            "selected_project",
            "selected_backlog_id",
        )
    conflicts: list[str] = []
    for field in fields:
        left = _string(existing.get(field))
        right = _string(supplied.get(field))
        if left and right and left != right:
            conflicts.append(field)
    return conflicts


def _merge_registry_lineage(
    existing: Mapping[str, Any],
    supplied: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in supplied.items():
        if value in ("", None, [], {}):
            merged.setdefault(str(key), value)
        else:
            merged[str(key)] = value
    return merged


def _ref_lineage_payloads(
    token: Mapping[str, Any],
    *,
    route_token_ref: str,
) -> dict[str, dict[str, Any]]:
    parent = _public_mapping(token.get("parent_route_lineage"))
    child = _public_mapping(token.get("child_route_lineage"))
    if parent and not child:
        child = _child_route_lineage(token, route_token_ref=route_token_ref)
    route_lineage = _public_mapping(token.get("route_lineage"))
    if parent and child and not route_lineage:
        route_lineage = {
            "schema_version": ROUTE_LINEAGE_SCHEMA_VERSION,
            "status": "parent_bound",
            "parent_route_id": _string(parent.get("route_id")),
            "parent_route_context_hash": _string(parent.get("route_context_hash")),
            "parent_prompt_contract_id": _string(parent.get("prompt_contract_id")),
            "child_route_id": _string(child.get("route_id")),
            "child_route_context_hash": _string(child.get("route_context_hash")),
            "child_prompt_contract_id": _string(child.get("prompt_contract_id")),
            "parent_route_lineage": dict(parent),
            "child_route_lineage": dict(child),
            "raw_route_token_persisted": False,
            "raw_session_token_persisted": False,
        }
    return {
        "parent_route_lineage": parent,
        "child_route_lineage": child,
        "route_lineage": route_lineage,
    }


def _with_registry_lineages(
    payload: dict[str, Any],
    row_dict: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(payload)
    for public_key, column in _REF_LINEAGE_COLUMNS.items():
        lineage = _json_loads_public_mapping(row_dict.get(column))
        if lineage:
            result[public_key] = lineage
    return result


def _row_scope(row_dict: Mapping[str, Any]) -> dict[str, Any]:
    return _json_loads_mapping(row_dict.get("scope_json"))


def _row_allowed_actions(row_dict: Mapping[str, Any]) -> list[str]:
    return _normalized_action_list(_json_loads_string_list(row_dict.get("allowed_actions_json")))


def _row_evidence_refs(row_dict: Mapping[str, Any]) -> list[str]:
    return _json_loads_string_list(row_dict.get("evidence_refs_json"))


def _row_target_files(row_dict: Mapping[str, Any]) -> list[str]:
    target_files = _json_loads_string_list(row_dict.get("target_files_json"))
    if target_files:
        return target_files
    scope = _row_scope(row_dict)
    return _dedupe(_string_list(scope.get("target_files")))


def _row_owned_files(row_dict: Mapping[str, Any]) -> list[str]:
    owned_files = _json_loads_string_list(row_dict.get("owned_files_json"))
    if owned_files:
        return owned_files
    scope = _row_scope(row_dict)
    owned_files = _dedupe(_string_list(scope.get("owned_files")))
    return owned_files or _row_target_files(row_dict)


def _backlog_target_files(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    backlog_id: str,
) -> list[str]:
    if not backlog_id:
        return []
    try:
        row = conn.execute(
            """
            SELECT target_files
            FROM backlog_bugs
            WHERE project_id=? AND bug_id=?
            """,
            (project_id, backlog_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if row is None:
        return []
    try:
        raw = row["target_files"]
    except (KeyError, TypeError, IndexError):
        raw = row[0]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = raw
        return _dedupe(_string_list(parsed))
    return _json_loads_string_list(raw)


def _legacy_row_identity_matches_target_files(
    row_dict: Mapping[str, Any],
    *,
    project_id: str,
    backlog_id: str,
    task_id: str,
    allowed_actions: Sequence[str],
    target_files: Sequence[str],
    project_root: Path | str | None,
) -> bool:
    issued_at = _parse_utc_datetime(row_dict.get("issued_at")) or _parse_utc_datetime(
        row_dict.get("created_at")
    )
    if issued_at is None:
        return False
    try:
        token = build_observer_write_route_token(
            project_id=project_id,
            backlog_id=backlog_id,
            task_id=task_id,
            target_files=target_files,
            allowed_actions=allowed_actions,
            ttl_hours=1.0,
            now=issued_at,
            project_root=project_root,
        )
    except (TypeError, ValueError):
        return False
    for field in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        stored = _string(row_dict.get(field))
        if stored and stored != _string(token.get(field)):
            return False
    return bool(
        _string(row_dict.get("route_context_hash"))
        and _string(row_dict.get("prompt_contract_id"))
    )


def _recover_legacy_row_target_files(
    conn: sqlite3.Connection,
    row_dict: Mapping[str, Any],
    *,
    project_id: str,
    backlog_id: str,
    task_id: str,
    allowed_actions: Sequence[str],
    requested_target_files: Sequence[str] | None,
    project_root: Path | str | None,
    route_token_ref: str,
) -> list[str]:
    """Recover old ref rows that predate persisted file-scope columns.

    Recovery only accepts candidate target files that reproduce the stored
    public route identity. This lets legacy refs renew without trusting caller
    supplied files or persisting/exposing raw tokens.
    """

    existing = _row_target_files(row_dict)
    if existing:
        return existing
    candidates: list[list[str]] = []
    for candidate in (
        _dedupe(_string_list(requested_target_files or [])),
        _backlog_target_files(conn, project_id=project_id, backlog_id=backlog_id),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if _legacy_row_identity_matches_target_files(
            row_dict,
            project_id=project_id,
            backlog_id=backlog_id,
            task_id=task_id,
            allowed_actions=allowed_actions,
            target_files=candidate,
            project_root=project_root,
        ):
            return candidate
    raise RouteTokenRefError(
        f"route_token_ref {route_token_ref!r} cannot renew without verified stored target_files",
        code="route_token_ref_target_files_missing",
        details={
            "field": "target_files",
            "route_token_ref": route_token_ref,
            "legacy_scope_recovery_attempted": bool(candidates),
        },
    )


def _registry_public_payload(
    row_dict: Mapping[str, Any],
    *,
    route_token_ref: str = "",
    expiry_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ref = _string(route_token_ref or row_dict.get("route_token_ref"))
    scope = _row_scope(row_dict)
    payload: dict[str, Any] = {
        "schema_version": REF_REGISTRY_SCHEMA_VERSION,
        "route_token_ref": ref,
        "route_id": _string(row_dict.get("route_id")),
        "route_context_hash": _string(row_dict.get("route_context_hash")),
        "prompt_contract_id": _string(row_dict.get("prompt_contract_id")),
        "prompt_contract_hash": _string(row_dict.get("prompt_contract_hash")),
        "visible_injection_manifest_hash": _string(
            row_dict.get("visible_injection_manifest_hash")
        ),
        "caller_role": _string(row_dict.get("caller_role")),
        "allowed_actions": _row_allowed_actions(row_dict),
        "evidence_refs": _row_evidence_refs(row_dict),
        "expires_at": _string(row_dict.get("expires_at")),
        "scope": scope,
        "target_files": _row_target_files(row_dict),
        "owned_files": _row_owned_files(row_dict),
        "status": _string(row_dict.get("status")),
        "resolved_from_ref": True,
    }
    if expiry_status:
        payload["expiry_status"] = dict(expiry_status)
    return _with_registry_lineages(payload, row_dict)


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
            target_files_json   TEXT NOT NULL DEFAULT '[]',
            owned_files_json    TEXT NOT NULL DEFAULT '[]',
            parent_route_lineage_json TEXT NOT NULL DEFAULT '{}',
            child_route_lineage_json TEXT NOT NULL DEFAULT '{}',
            route_lineage_json TEXT NOT NULL DEFAULT '{}',
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
    existing_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(observer_route_token_refs)").fetchall()
    }
    for column in _REF_LINEAGE_COLUMNS.values():
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE observer_route_token_refs "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'"
            )
    for column in _REF_SCOPE_LIST_COLUMNS.values():
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE observer_route_token_refs "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
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
    target_files = _dedupe(_string_list(token.get("target_files")))
    owned_files = _dedupe(_string_list(token.get("owned_files"))) or target_files
    lineage_payloads = _ref_lineage_payloads(token, route_token_ref=route_token_ref)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _ensure_ref_registry_schema(conn)
    with _REF_REGISTRY_LOCK:
        # Check for existing entry
        row = conn.execute(
            "SELECT * FROM observer_route_token_refs "
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
            missing_lineage_columns = [
                column
                for public_key, column in _REF_LINEAGE_COLUMNS.items()
                if lineage_payloads[public_key]
                and not _json_loads_public_mapping(dict(row).get(column))
            ]
            row_dict = dict(row)
            missing_scope_columns = [
                column
                for value, column in (
                    (target_files, "target_files_json"),
                    (owned_files, "owned_files_json"),
                )
                if value and not _json_loads_string_list(row_dict.get(column))
            ]
            if missing_lineage_columns or missing_scope_columns:
                conn.execute(
                    """
                    UPDATE observer_route_token_refs
                    SET parent_route_lineage_json=?,
                        child_route_lineage_json=?,
                        route_lineage_json=?,
                        target_files_json=?,
                        owned_files_json=?
                    WHERE project_id=? AND route_token_ref=?
                    """,
                    (
                        _json_dumps_public_mapping(
                            lineage_payloads["parent_route_lineage"]
                        ),
                        _json_dumps_public_mapping(
                            lineage_payloads["child_route_lineage"]
                        ),
                        _json_dumps_public_mapping(lineage_payloads["route_lineage"]),
                        _json_dumps_string_list(target_files),
                        _json_dumps_string_list(owned_files),
                        project_id,
                        route_token_ref,
                    ),
                )
                conn.commit()
            # Idempotent re-issue: same token, already registered.
            return

        conn.execute(
            """
            INSERT INTO observer_route_token_refs
                (project_id, route_token_ref, token_digest, salt,
                 route_id, route_context_hash, prompt_contract_id,
                 prompt_contract_hash, visible_injection_manifest_hash,
                 backlog_id, task_id, caller_role,
                 allowed_actions_json, expires_at, evidence_refs_json,
                 scope_json, target_files_json, owned_files_json,
                 parent_route_lineage_json, child_route_lineage_json,
                 route_lineage_json, status, issued_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                _json_dumps_string_list(target_files),
                _json_dumps_string_list(owned_files),
                _json_dumps_public_mapping(lineage_payloads["parent_route_lineage"]),
                _json_dumps_public_mapping(lineage_payloads["child_route_lineage"]),
                _json_dumps_public_mapping(lineage_payloads["route_lineage"]),
                REF_STATUS_ACTIVE,
                _string(token.get("issued_at")) or now_str,
                now_str,
            ),
        )
        conn.commit()


def persist_route_token_ref_lineage(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    route_token_ref: str,
    parent_route_lineage: Mapping[str, Any],
    child_route_lineage: Mapping[str, Any],
    route_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach server-validated parent/child route lineage to an active ref.

    This stores only public route-lineage fields and refuses to overwrite an
    existing non-matching registry lineage. Close precheck may consume registry
    lineage, but it must never trust event-local lineage directly.
    """

    project_id = _string(project_id)
    route_token_ref = _string(route_token_ref)
    parent = _public_mapping(parent_route_lineage)
    child = _public_mapping(child_route_lineage)
    if not project_id or not route_token_ref:
        raise ValueError("project_id and route_token_ref are required")
    if not parent or not child:
        raise ValueError("parent_route_lineage and child_route_lineage are required")

    lineage_payloads = _ref_lineage_payloads(
        {
            "parent_route_lineage": parent,
            "child_route_lineage": child,
            "route_lineage": _public_mapping(route_lineage),
        },
        route_token_ref=route_token_ref,
    )

    _ensure_ref_registry_schema(conn)
    with _REF_REGISTRY_LOCK:
        row = conn.execute(
            "SELECT * FROM observer_route_token_refs "
            "WHERE project_id=? AND route_token_ref=?",
            (project_id, route_token_ref),
        ).fetchone()
        if row is None:
            return {}
        row_dict = dict(row)
        status = _string(row_dict.get("status"))
        if status != REF_STATUS_ACTIVE:
            raise RouteTokenRefError(
                f"route_token_ref {route_token_ref!r} is not active (status={status!r}); "
                "lineage attach refused"
            )
        for field in ("route_id", "route_context_hash", "prompt_contract_id"):
            stored = _string(row_dict.get(field))
            supplied = _string(child.get(field))
            if stored and supplied and stored != supplied:
                raise RouteTokenRefError(
                    f"route_token_ref lineage mismatch: child {field} {supplied!r} "
                    f"does not match registered {stored!r}"
                )

        final_payloads: dict[str, dict[str, Any]] = {}
        changed = False
        for public_key, column in _REF_LINEAGE_COLUMNS.items():
            existing = _json_loads_public_mapping(row_dict.get(column))
            supplied = lineage_payloads[public_key]
            conflicts = (
                _lineage_conflicting_fields(
                    existing,
                    supplied,
                    public_key=public_key,
                )
                if existing and supplied
                else []
            )
            if conflicts:
                raise RouteTokenRefError(
                    f"route_token_ref lineage mismatch: registered {public_key} "
                    f"conflicts on {', '.join(conflicts)}"
                )
            final_payloads[public_key] = (
                _merge_registry_lineage(existing, supplied)
                if existing and supplied
                else existing or supplied
            )
            if final_payloads[public_key] != existing:
                changed = True

        if changed:
            conn.execute(
                """
                UPDATE observer_route_token_refs
                SET parent_route_lineage_json=?,
                    child_route_lineage_json=?,
                    route_lineage_json=?
                WHERE project_id=? AND route_token_ref=?
                """,
                (
                    _json_dumps_public_mapping(final_payloads["parent_route_lineage"]),
                    _json_dumps_public_mapping(final_payloads["child_route_lineage"]),
                    _json_dumps_public_mapping(final_payloads["route_lineage"]),
                    project_id,
                    route_token_ref,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM observer_route_token_refs "
                "WHERE project_id=? AND route_token_ref=?",
                (project_id, route_token_ref),
            ).fetchone()
            row_dict = dict(row) if row is not None else row_dict

    return _registry_public_payload(row_dict, route_token_ref=route_token_ref)


def resolve_route_token_ref(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    route_token_ref: str,
    route_id: str = "",
    route_context_hash: str = "",
    prompt_contract_id: str = "",
    task_id: str = "",
    backlog_id: str = "",
    now: datetime | None = None,
    renew_within_seconds: int = 0,
) -> dict[str, Any] | None:
    """Resolve a route_token_ref to its stored public identity.

    Returns a dict of public token fields sufficient for the gate (route identity,
    allowed_actions, scope, expiry, evidence_refs) **without** the raw token body.
    Returns ``None`` when the ref is unknown.

    Fails closed:
    - Unknown ref → ``None`` (caller must treat as gate failure).
    - ``status != 'active'`` (superseded / expired) → raises
      ``RouteTokenRefError`` with the status.
    - Identity mismatch (route_id / route_context_hash / prompt_contract_id /
      task_id / backlog_id when supplied) → raises ``RouteTokenRefError``.
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
        stored_backlog = backlog_id or _string(row_dict.get("backlog_id"))
        stored_task = task_id or _string(row_dict.get("task_id"))
        parent_lineage = _json_loads_public_mapping(
            row_dict.get(_REF_LINEAGE_COLUMNS["parent_route_lineage"])
        )
        raise RouteTokenRefError(
            f"route_token_ref {route_token_ref!r} is not active (status={status!r}); "
            "ref resolution refused",
            code="route_token_ref_not_active",
            details={
                "status": status,
                "next_action": route_token_ref_renewal_next_action(
                    project_id=project_id,
                    backlog_id=stored_backlog,
                    task_id=stored_task,
                    route_token_ref=route_token_ref,
                    reason=status or "not_active",
                    allowed_actions=_row_allowed_actions(row_dict),
                    target_files=_row_target_files(row_dict),
                    owned_files=_row_owned_files(row_dict),
                    evidence_refs=_row_evidence_refs(row_dict),
                    parent_route_identity=parent_lineage or None,
                ),
            },
        )

    # Identity binding checks — only when the caller supplies non-empty values.
    if route_id:
        stored_route_id = _string(row_dict.get("route_id"))
        # F2-BINDING-FAIL-CLOSED: when the caller supplies route_id and the stored
        # entry has an EMPTY value, refuse — the binding cannot be corroborated.
        # (A stored non-empty value that differs is also refused, as before.)
        if not stored_route_id:
            raise RouteTokenRefError(
                f"route_token_ref binding cannot be corroborated: route_id {route_id!r} "
                "was supplied but the registered entry has no stored route_id"
            )
        if stored_route_id != _string(route_id):
            raise RouteTokenRefError(
                f"route_token_ref identity mismatch: route_id {route_id!r} does not "
                f"match registered {stored_route_id!r}"
            )
    if route_context_hash:
        stored_rch = _string(row_dict.get("route_context_hash"))
        # F2-BINDING-FAIL-CLOSED: same logic for route_context_hash.
        if not stored_rch:
            raise RouteTokenRefError(
                "route_token_ref binding cannot be corroborated: route_context_hash "
                "was supplied but the registered entry has no stored route_context_hash"
            )
        if stored_rch != _string(route_context_hash):
            raise RouteTokenRefError(
                "route_token_ref identity mismatch: route_context_hash does not match"
            )
    if prompt_contract_id:
        stored_prompt = _string(row_dict.get("prompt_contract_id"))
        if not stored_prompt:
            raise RouteTokenRefError(
                "route_token_ref binding cannot be corroborated: prompt_contract_id "
                "was supplied but the registered entry has no stored prompt_contract_id"
            )
        if stored_prompt != _string(prompt_contract_id):
            raise RouteTokenRefError(
                "route_token_ref identity mismatch: prompt_contract_id does not match"
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
    expiry_status = route_token_ref_expiry_status(
        expires_at,
        now=now,
        renew_within_seconds=renew_within_seconds,
        project_id=project_id,
        backlog_id=backlog_id or _string(row_dict.get("backlog_id")),
        task_id=task_id or _string(row_dict.get("task_id")),
        route_token_ref=route_token_ref,
    )
    if expiry_status.get("expired"):
        raise RouteTokenRefError(
            f"route_token_ref {route_token_ref!r} has expired (expires_at={expires_at!r})",
            code="route_token_ref_expired",
            details={
                "expiry_status": expiry_status,
                "next_action": expiry_status.get("next_action"),
            },
        )
    if renew_within_seconds and expiry_status.get("near_expiry"):
        raise RouteTokenRefError(
            f"route_token_ref {route_token_ref!r} is near expiry (expires_at={expires_at!r})",
            code="route_token_ref_near_expiry",
            details={
                "expiry_status": expiry_status,
                "next_action": expiry_status.get("next_action"),
            },
        )

    return _registry_public_payload(
        row_dict,
        route_token_ref=route_token_ref,
        expiry_status=expiry_status,
    )


def verify_route_token_binding(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    token: Mapping[str, Any],
    route_token_ref: str = "",
    route_id: str = "",
    route_context_hash: str = "",
    prompt_contract_id: str = "",
    task_id: str = "",
    backlog_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify a presented full token against active server-issued rows.

    Full route-token objects returned by ``/issue`` intentionally do not embed a
    ``route_token_ref``. Binding therefore starts from the token's route
    identity (``route_id`` + ``route_context_hash`` + ``prompt_contract_id``),
    finds active registry rows with that identity, and then requires an exact
    salted digest match for the full canonical token object.
    """

    project_id = _string(project_id)
    if not project_id:
        raise RouteTokenRefError("project_id is required for route_token binding")
    if not isinstance(token, Mapping):
        raise RouteTokenRefError("route_token binding requires a full token mapping")

    token_route_id = _string(token.get("route_id"))
    token_route_context_hash = _string(token.get("route_context_hash"))
    token_prompt_contract_id = _string(token.get("prompt_contract_id"))
    if not token_route_id or not token_route_context_hash or not token_prompt_contract_id:
        raise RouteTokenRefError(
            "route_token binding requires route_id, route_context_hash, and prompt_contract_id"
        )
    if route_id and _string(route_id) != token_route_id:
        raise RouteTokenRefError("route_token request route_id does not match token identity")
    if route_context_hash and _string(route_context_hash) != token_route_context_hash:
        raise RouteTokenRefError(
            "route_token request route_context_hash does not match token identity"
        )
    if prompt_contract_id and _string(prompt_contract_id) != token_prompt_contract_id:
        raise RouteTokenRefError(
            "route_token request prompt_contract_id does not match token identity"
        )

    explicit_ref = _string(route_token_ref or token.get("route_token_ref"))
    try:
        _ensure_ref_registry_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM observer_route_token_refs
            WHERE project_id=?
              AND route_id=?
              AND route_context_hash=?
              AND prompt_contract_id=?
            ORDER BY created_at DESC, issued_at DESC, route_token_ref DESC
            """,
            (
                project_id,
                token_route_id,
                token_route_context_hash,
                token_prompt_contract_id,
            ),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise RouteTokenRefError(
            "route_token_ref registry unavailable; server-issued binding refused"
        ) from exc

    if not rows:
        raise RouteTokenRefError(
            "no active observer_route_token_refs row matches the presented route token identity"
        )

    active_rows: list[dict[str, Any]] = []
    inactive_statuses: list[str] = []
    for row in rows:
        row_dict = dict(row)
        status = _string(row_dict.get("status"))
        if status == REF_STATUS_ACTIVE:
            active_rows.append(row_dict)
        else:
            inactive_statuses.append(status or "inactive")
    if not active_rows:
        statuses = ", ".join(sorted(set(inactive_statuses))) or "inactive"
        raise RouteTokenRefError(
            "route_token identity has no active observer_route_token_refs row "
            f"(status={statuses}); server-issued binding refused"
        )

    token_scope = token.get("scope") if isinstance(token.get("scope"), Mapping) else {}

    def _scope_value(source: Mapping[str, Any], *names: str) -> str:
        for name in names:
            value = _string(source.get(name))
            if value:
                return value
        return ""

    def _loads_mapping(raw: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    def _loads_list(raw: Any) -> list[str]:
        try:
            parsed = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            parsed = []
        return _string_list(parsed)

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)

    saw_expired = False
    saw_scope_mismatch = False
    saw_explicit_ref_mismatch = False
    saw_allowed_superset = False
    saw_digest_mismatch = False

    for row_dict in active_rows:
        stored_ref = _string(row_dict.get("route_token_ref"))
        if explicit_ref and explicit_ref != stored_ref:
            saw_explicit_ref_mismatch = True
            continue

        stored_scope = _loads_mapping(row_dict.get("scope_json"))
        stored_project = _scope_value(stored_scope, "project_id") or project_id
        token_project = _scope_value(token_scope, "project_id") or _string(
            token.get("project_id")
        )
        stored_backlog = _string(row_dict.get("backlog_id")) or _scope_value(
            stored_scope, "backlog_id", "bug_id"
        )
        token_backlog = _scope_value(token_scope, "backlog_id", "bug_id") or _string(
            token.get("backlog_id") or token.get("bug_id")
        )
        stored_task = _string(row_dict.get("task_id")) or _scope_value(
            stored_scope, "task_id"
        )
        token_task = _scope_value(token_scope, "task_id") or _string(token.get("task_id"))
        scope_ok = (
            token_project == stored_project
            and project_id == stored_project
            and (not stored_backlog or token_backlog == stored_backlog)
            and (not backlog_id or not stored_backlog or _string(backlog_id) == stored_backlog)
            and (not stored_task or token_task == stored_task)
        )
        if not scope_ok:
            saw_scope_mismatch = True
            continue

        stored_expires_at = _string(row_dict.get("expires_at"))
        if not stored_expires_at:
            saw_expired = True
            continue
        try:
            expires_dt = datetime.fromisoformat(stored_expires_at.replace("Z", "+00:00"))
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if now_dt >= expires_dt.astimezone(timezone.utc):
                saw_expired = True
                continue
        except (TypeError, ValueError):
            saw_expired = True
            continue

        stored_allowed_actions = _normalized_action_list(
            _loads_list(row_dict.get("allowed_actions_json"))
        )
        token_allowed_actions = _normalized_action_list(token.get("allowed_actions"))
        token_set = set(token_allowed_actions)
        stored_set = set(stored_allowed_actions)
        if not token_set.issubset(stored_set):
            saw_allowed_superset = True
            continue

        expected_digest = _string(row_dict.get("token_digest"))
        actual_digest = _token_digest(token, _string(row_dict.get("salt")))
        if not expected_digest or not hmac.compare_digest(actual_digest, expected_digest):
            saw_digest_mismatch = True
            continue

        try:
            evidence_refs = json.loads(row_dict.get("evidence_refs_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            evidence_refs = []
        result = {
            "schema_version": REF_REGISTRY_SCHEMA_VERSION,
            "server_issued_binding": True,
            "binding_source": "observer_route_token_refs",
            "route_token_ref": stored_ref,
            "route_id": token_route_id,
            "route_context_hash": token_route_context_hash,
            "prompt_contract_id": token_prompt_contract_id,
            "prompt_contract_hash": _string(row_dict.get("prompt_contract_hash")),
            "visible_injection_manifest_hash": _string(
                row_dict.get("visible_injection_manifest_hash")
            ),
            "caller_role": _string(row_dict.get("caller_role")),
            "allowed_actions": stored_allowed_actions,
            "evidence_refs": _string_list(evidence_refs),
            "expires_at": stored_expires_at,
            "scope": stored_scope,
            "status": REF_STATUS_ACTIVE,
        }
        for public_key, column in _REF_LINEAGE_COLUMNS.items():
            lineage = _json_loads_public_mapping(row_dict.get(column))
            if not lineage and isinstance(token.get(public_key), Mapping):
                lineage = dict(token.get(public_key) or {})
            if lineage:
                result[public_key] = lineage
        return result

    if saw_allowed_superset:
        raise RouteTokenRefError(
            "route_token allowed_actions exceed registered server-issued grant"
        )
    if saw_digest_mismatch:
        raise RouteTokenRefError(
            "route_token digest mismatch for registered server-issued binding"
        )
    if saw_scope_mismatch:
        raise RouteTokenRefError(
            "route_token scope does not match registered server-issued binding"
        )
    if saw_expired:
        raise RouteTokenRefError(
            "route_token identity only matched expired/invalid observer_route_token_refs rows"
        )
    if saw_explicit_ref_mismatch:
        raise RouteTokenRefError(
            "route_token_ref does not match any active row for the presented route token identity"
        )
    raise RouteTokenRefError(
        "route_token identity did not match an active server-issued binding"
    )


def _scope_value(source: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = _string(source.get(name))
        if value:
            return value
    return ""


def _subset_or_same(
    requested: Sequence[str],
    existing: Sequence[str],
    *,
    field: str,
    route_token_ref: str,
) -> list[str]:
    existing_list = _dedupe(_string_list(existing))
    requested_list = _dedupe(_string_list(requested))
    if not requested_list:
        if existing_list:
            return existing_list
        raise RouteTokenRefError(
            f"route_token_ref {route_token_ref!r} cannot renew without stored {field}",
            code=f"route_token_ref_{field}_missing",
            details={"field": field, "route_token_ref": route_token_ref},
        )
    missing = sorted(set(requested_list) - set(existing_list))
    if missing:
        raise RouteTokenRefError(
            f"route_token_ref renewal cannot widen {field}: {', '.join(missing)}",
            code=f"route_token_ref_{field}_widening_refused",
            details={
                "field": field,
                "route_token_ref": route_token_ref,
                "requested": requested_list,
                "existing": existing_list,
                "widening_values": missing,
            },
        )
    return requested_list


def _actions_subset_or_same(
    requested: Sequence[str] | None,
    existing: Sequence[str],
    *,
    route_token_ref: str,
) -> list[str]:
    existing_actions = _normalized_action_list(existing)
    if not requested:
        if existing_actions:
            return existing_actions
        raise RouteTokenRefError(
            f"route_token_ref {route_token_ref!r} cannot renew without stored allowed_actions",
            code="route_token_ref_allowed_actions_missing",
            details={"route_token_ref": route_token_ref},
        )
    requested_actions = _normalized_action_list(requested)
    missing = sorted(set(requested_actions) - set(existing_actions))
    if missing:
        raise RouteTokenRefError(
            "route_token_ref renewal cannot widen allowed_actions: "
            + ", ".join(missing),
            code="route_token_ref_allowed_actions_widening_refused",
            details={
                "route_token_ref": route_token_ref,
                "requested": requested_actions,
                "existing": existing_actions,
                "widening_values": missing,
            },
        )
    return requested_actions


def renew_route_token_ref(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    route_token_ref: str,
    backlog_id: str = "",
    task_id: str = "",
    caller_role: str = "",
    allowed_actions: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    owned_files: Sequence[str] | None = None,
    ttl_hours: float = 24.0,
    renew_within_seconds: int = ROUTE_TOKEN_REF_RENEW_WITHIN_SECONDS,
    now: datetime | None = None,
    evidence_refs: Sequence[str] | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Renew a same-scope route_token_ref and supersede the previous ref."""

    project_id = _string(project_id)
    old_ref = _string(route_token_ref)
    if not project_id or not old_ref:
        raise ValueError("project_id and route_token_ref are required")

    _ensure_ref_registry_schema(conn)
    with _REF_REGISTRY_LOCK:
        row = conn.execute(
            "SELECT * FROM observer_route_token_refs WHERE project_id=? AND route_token_ref=?",
            (project_id, old_ref),
        ).fetchone()
        if row is None:
            raise RouteTokenRefError(
                f"route_token_ref {old_ref!r} is unknown; renewal refused",
                code="route_token_ref_unknown",
                details={"route_token_ref": old_ref},
            )
        row_dict = dict(row)
        status = _string(row_dict.get("status"))
        if status == REF_STATUS_SUPERSEDED:
            stored_parent_lineage = _json_loads_public_mapping(
                row_dict.get(_REF_LINEAGE_COLUMNS["parent_route_lineage"])
            )
            raise RouteTokenRefError(
                f"route_token_ref {old_ref!r} is superseded; renewal refused",
                code="route_token_ref_superseded",
                details={
                    "route_token_ref": old_ref,
                    "status": status,
                    "next_action": route_token_ref_same_scope_issue_next_action(
                        project_id=project_id,
                        backlog_id=backlog_id or _string(row_dict.get("backlog_id")),
                        task_id=task_id or _string(row_dict.get("task_id")),
                        route_token_ref=old_ref,
                        reason=status,
                        allowed_actions=_row_allowed_actions(row_dict),
                        target_files=_row_target_files(row_dict),
                        owned_files=_row_owned_files(row_dict),
                        evidence_refs=_row_evidence_refs(row_dict),
                        parent_route_identity=stored_parent_lineage or None,
                    ),
                },
            )
        if status not in {REF_STATUS_ACTIVE, REF_STATUS_EXPIRED}:
            raise RouteTokenRefError(
                f"route_token_ref {old_ref!r} status {status!r} cannot be renewed",
                code="route_token_ref_status_not_renewable",
                details={"route_token_ref": old_ref, "status": status},
            )

        scope = _row_scope(row_dict)
        stored_project = _scope_value(scope, "project_id") or project_id
        stored_backlog = _string(row_dict.get("backlog_id")) or _scope_value(
            scope, "backlog_id", "bug_id"
        )
        stored_task = _string(row_dict.get("task_id")) or _scope_value(scope, "task_id")
        if stored_project != project_id:
            raise RouteTokenRefError(
                "route_token_ref renewal project scope mismatch",
                code="route_token_ref_project_scope_mismatch",
                details={
                    "route_token_ref": old_ref,
                    "stored_project_id": stored_project,
                    "project_id": project_id,
                },
            )
        requested_backlog = _string(backlog_id) or stored_backlog
        requested_task = _string(task_id) or stored_task
        if not stored_backlog or requested_backlog != stored_backlog:
            raise RouteTokenRefError(
                "route_token_ref renewal backlog scope mismatch",
                code="route_token_ref_backlog_scope_mismatch",
                details={
                    "route_token_ref": old_ref,
                    "stored_backlog_id": stored_backlog,
                    "backlog_id": requested_backlog,
                },
            )
        if not stored_task or requested_task != stored_task:
            raise RouteTokenRefError(
                "route_token_ref renewal task scope mismatch",
                code="route_token_ref_task_scope_mismatch",
                details={
                    "route_token_ref": old_ref,
                    "stored_task_id": stored_task,
                    "task_id": requested_task,
                },
            )

        stored_role = _string(row_dict.get("caller_role"))
        requested_role = _string(caller_role) or stored_role
        if stored_role and requested_role and requested_role != stored_role:
            raise RouteTokenRefError(
                "route_token_ref renewal caller_role mismatch",
                code="route_token_ref_caller_role_mismatch",
                details={
                    "route_token_ref": old_ref,
                    "stored_caller_role": stored_role,
                    "caller_role": requested_role,
                },
            )

        renewed_actions = _actions_subset_or_same(
            allowed_actions,
            _row_allowed_actions(row_dict),
            route_token_ref=old_ref,
        )
        stored_target_files = _recover_legacy_row_target_files(
            conn,
            row_dict,
            project_id=project_id,
            backlog_id=stored_backlog,
            task_id=stored_task,
            allowed_actions=renewed_actions,
            requested_target_files=target_files,
            project_root=project_root,
            route_token_ref=old_ref,
        )
        stored_owned_files = _row_owned_files(row_dict) or list(stored_target_files)
        renewed_target_files = _subset_or_same(
            target_files or [],
            stored_target_files,
            field="target_files",
            route_token_ref=old_ref,
        )
        renewed_owned_files = _subset_or_same(
            owned_files or [],
            stored_owned_files,
            field="owned_files",
            route_token_ref=old_ref,
        )

        parent_lineage = _json_loads_public_mapping(
            row_dict.get(_REF_LINEAGE_COLUMNS["parent_route_lineage"])
        )
        evidence = _dedupe(
            [
                *_row_evidence_refs(row_dict),
                *(_string_list(evidence_refs) if evidence_refs is not None else []),
                f"renewed_from:{old_ref}",
            ]
        )
        issued = issue_observer_write_route_context(
            project_id=project_id,
            backlog_id=stored_backlog,
            task_id=stored_task,
            target_files=renewed_target_files,
            allowed_actions=renewed_actions,
            ttl_hours=ttl_hours,
            now=now,
            evidence_refs=evidence,
            project_root=project_root,
            parent_route_identity=parent_lineage or None,
            parent_route_token_ref=_string(parent_lineage.get("route_token_ref")),
        )
        new_ref = _string(issued.get("route_token_ref"))
        if not new_ref or new_ref == old_ref:
            raise RouteTokenRefError(
                "route_token_ref renewal did not produce a fresh ref",
                code="route_token_ref_renewal_not_fresh",
                details={"route_token_ref": old_ref, "new_route_token_ref": new_ref},
            )
        token = issued.get("route_token")
        if not isinstance(token, dict):
            raise RouteTokenRefError(
                "route_token_ref renewal failed to mint a token",
                code="route_token_ref_renewal_mint_failed",
                details={"route_token_ref": old_ref},
            )
        token["owned_files"] = list(renewed_owned_files)
        previous_route_identity = {
            "route_id": _string(row_dict.get("route_id")),
            "route_context_hash": _string(row_dict.get("route_context_hash")),
            "prompt_contract_id": _string(row_dict.get("prompt_contract_id")),
            "prompt_contract_hash": _string(row_dict.get("prompt_contract_hash")),
            "visible_injection_manifest_hash": _string(
                row_dict.get("visible_injection_manifest_hash")
            ),
            "route_token_ref": old_ref,
        }
        renewed_route_identity = {
            "route_id": _string(token.get("route_id")),
            "route_context_hash": _string(token.get("route_context_hash")),
            "prompt_contract_id": _string(token.get("prompt_contract_id")),
            "prompt_contract_hash": _string(token.get("prompt_contract_hash")),
            "visible_injection_manifest_hash": _string(
                token.get("visible_injection_manifest_hash")
            ),
            "route_token_ref": new_ref,
        }
        route_lineage = _public_mapping(token.get("route_lineage"))
        route_lineage.setdefault("schema_version", ROUTE_LINEAGE_SCHEMA_VERSION)
        route_lineage["route_token_ref_renewed"] = True
        route_lineage["renewal_proof"] = {
            "schema_version": REF_RENEWAL_PROOF_SCHEMA_VERSION,
            "status": "renewed",
            "source": "renew_route_token_ref",
            "previous_route_token_ref": old_ref,
            "route_token_ref": new_ref,
            "scope": {
                "project_id": project_id,
                "backlog_id": stored_backlog,
                "task_id": stored_task,
            },
            "previous_route_identity": previous_route_identity,
            "route_identity": renewed_route_identity,
            "raw_route_token_persisted": False,
            "raw_session_token_persisted": False,
        }
        token["route_lineage"] = route_lineage
        persist_route_token_ref(
            conn,
            project_id=project_id,
            route_token_ref=new_ref,
            token=token,
        )
        conn.execute(
            "UPDATE observer_route_token_refs SET status=? "
            "WHERE project_id=? AND route_token_ref=? AND status IN (?, ?)",
            (
                REF_STATUS_SUPERSEDED,
                project_id,
                old_ref,
                REF_STATUS_ACTIVE,
                REF_STATUS_EXPIRED,
            ),
        )
        conn.commit()
        new_row = conn.execute(
            "SELECT * FROM observer_route_token_refs WHERE project_id=? AND route_token_ref=?",
            (project_id, new_ref),
        ).fetchone()
        new_row_dict = dict(new_row) if new_row is not None else {}

    expiry_status = route_token_ref_expiry_status(
        new_row_dict.get("expires_at"),
        now=now,
        renew_within_seconds=renew_within_seconds,
        project_id=project_id,
        backlog_id=stored_backlog,
        task_id=stored_task,
        route_token_ref=new_ref,
    )
    renewed_public = _registry_public_payload(
        new_row_dict,
        route_token_ref=new_ref,
        expiry_status=expiry_status,
    )
    return {
        "schema_version": REF_RENEWAL_SCHEMA_VERSION,
        "ok": True,
        "project_id": project_id,
        "previous_route_token_ref": old_ref,
        "route_token_ref": new_ref,
        "previous_status": status,
        "status": "renewed",
        "renewed": True,
        "superseded_previous_ref": True,
        "previous_expires_at": _string(row_dict.get("expires_at")),
        "expires_at": _string(new_row_dict.get("expires_at")),
        "scope": {
            "project_id": project_id,
            "backlog_id": stored_backlog,
            "task_id": stored_task,
        },
        "allowed_actions": list(renewed_actions),
        "target_files": list(renewed_target_files),
        "owned_files": list(renewed_owned_files),
        "route_identity": {
            "route_id": renewed_public.get("route_id", ""),
            "route_context_hash": renewed_public.get("route_context_hash", ""),
            "prompt_contract_id": renewed_public.get("prompt_contract_id", ""),
            "prompt_contract_hash": renewed_public.get("prompt_contract_hash", ""),
            "visible_injection_manifest_hash": renewed_public.get(
                "visible_injection_manifest_hash", ""
            ),
            "route_token_ref": new_ref,
        },
        "renewed_route_token_ref": renewed_public,
        "merge_queue_id": issued.get("merge_queue_id", ""),
        "execute_backlog_row_payload": issued.get("execute_backlog_row_payload", {}),
        "expiry_status": expiry_status,
        "raw_route_token_required": False,
        "raw_route_token_exposed": False,
        "raw_route_token_persisted": False,
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
            "WHERE project_id=? AND route_token_ref=? AND status IN (?, ?)",
            (
                REF_STATUS_SUPERSEDED,
                project_id,
                route_token_ref,
                REF_STATUS_ACTIVE,
                REF_STATUS_EXPIRED,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        return False


class RouteTokenRefError(Exception):
    """Raised for route_token_ref resolution, renewal, and binding failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = _string(code) or "route_token_ref_error"
        self.details = dict(details or {})
        super().__init__(message)
