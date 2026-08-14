"""Thin observer-runtime bridge into governed CLI Agent Service admission."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .launchers import WORKER_AUTH_ENV_KEYS, scrub_host_envelope_payload
from .service import (
    DEFAULT_SOCKET_TIMEOUT_SECONDS,
    ServiceError,
    ServicePaths,
    ServiceUnavailableError,
    mcp_application_mapping_blocks,
    request_service,
    scrub_host_secret_values,
    unwrap_mcp_application_response,
)


GUIDED_RUNTIME_DISPATCH_SCHEMA_VERSION = "cli_agent_service.guided_runtime_dispatch.v1"
RUNTIME_CONTEXT_HOST_ORCHESTRATION_SCHEMA_VERSION = (
    "cli_agent_service.runtime_context_host_orchestration.v1"
)
RUNTIME_CONTEXT_GRAPH_CONTINUATION_SCHEMA_VERSION = (
    "cli_agent_service.runtime_context_graph_continuation.v1"
)

_PLACEHOLDER = re.compile(r"<[^<>]+>")
_INITIAL_JOIN_TOOL_FIELDS = frozenset(
    """project_id runtime_context_id task_id backlog_id parent_task_id
    contract_execution_id target_project_root worker_id worker_slot_id agent_id
    actual_host_worker_id worker_session_id host_session_id host_startup_id
    session_token_ref route_id route_context_hash prompt_contract_id
    prompt_contract_hash route_token_ref visible_injection_manifest_hash
    ttl_seconds reason now_iso""".split()
)
_READ_RECEIPT_TOOL_FIELDS = frozenset(
    """project_id runtime_context_id task_id backlog_id parent_task_id
    contract_execution_id contract_chain_id parent_contract_execution_id
    successor_contract_execution_id contract_revision_id contract_hash context_hash
    worker_role worker_id worker_slot_id target_project_root session_token
    session_token_ref fence_token event_type event_kind phase actor actor_role
    actor_session_principal status read_receipt_hash receipt_hash launch_text_hash
    acknowledged_at contract_context_read_receipt route_id route_context_hash
    prompt_contract_id prompt_contract_hash route_token_ref
    visible_injection_manifest_hash graph_trace_id payload verification artifact_refs
    trace_id commit_sha now_iso""".split()
)
_STARTUP_TOOL_FIELDS = frozenset(
    """project_id actual_cwd actual_git_root actual_host_worker_id agent_id
    base_commit branch branch_head branch_ref fence_token filer_principal harness_type
    head_commit host_session_id host_startup_id launch_text_hash merge_queue_id now_iso
    observer_command_id owned_files parent_task_id prompt_contract_hash
    prompt_contract_id read_receipt_event_id read_receipt_hash role route_context_hash
    route_id route_token_ref runtime_context_id session_token session_token_ref
    session_token_surrogate startup_source target_head_commit task_id
    visible_injection_manifest_hash worker_id worker_role worker_session_id
    worker_transcript_path worker_transcript_ref""".split()
)
_HOST_PRIVACY_FLAGS = {
    "raw_session_token_exposed": False,
    "raw_fence_token_exposed": False,
    "raw_session_token_persisted": False,
    "raw_fence_token_persisted": False,
}
_HOST_REPLACEMENT_FIELDS_BY_SOURCE = {
    "host_identity": frozenset(
        """project_id worker_session_id host_session_id host_startup_id now_iso
        read_receipt_hash worker_transcript_ref worker_transcript_path filer_principal
        actor_session_principal launch_text_hash head_commit actual_cwd actual_git_root
        harness_type agent_id actual_host_worker_id worker_id worker_slot_id""".split()
    ),
    "worker_guide": frozenset(
        """contract_execution_id contract_hash context_hash agent_id
        actual_host_worker_id worker_id worker_slot_id parent_task_id
        target_project_root branch branch_ref base_commit target_head_commit
        merge_queue_id observer_command_id route_id route_context_hash
        prompt_contract_id prompt_contract_hash route_token_ref
        visible_injection_manifest_hash""".split()
    ),
    "host_computed": frozenset(
        """project_id reason worker_session_id host_session_id host_startup_id
        worker_transcript_ref worker_transcript_path filer_principal
        actor_session_principal acknowledged_at now_iso read_receipt_hash receipt_hash
        launch_text_hash head_commit actual_cwd actual_git_root harness_type""".split()
    ),
    "initial_join": frozenset("session_token fence_token session_token_ref".split()),
    "read_receipt": frozenset(
        "read_receipt_hash receipt_hash read_receipt_event_id".split()
    ),
}
_HOST_REPLACEMENT_FIELDS = frozenset().union(
    *_HOST_REPLACEMENT_FIELDS_BY_SOURCE.values()
)
_INITIAL_FORCE_FIELDS = frozenset(
    "project_id reason worker_session_id host_session_id host_startup_id now_iso".split()
)
_INITIAL_REQUIRED_FIELDS = tuple(
    """project_id runtime_context_id task_id reason agent_id actual_host_worker_id
    worker_session_id host_session_id host_startup_id""".split()
)
_RECEIPT_FORCE_FIELDS = frozenset(
    """project_id session_token fence_token session_token_ref read_receipt_hash
    receipt_hash launch_text_hash acknowledged_at now_iso
    actor_session_principal""".split()
)
_RECEIPT_REQUIRED_FIELDS = tuple(
    """project_id runtime_context_id task_id parent_task_id session_token fence_token
    session_token_ref read_receipt_hash""".split()
)
_STARTUP_FORCE_FIELDS = frozenset(
    """project_id session_token fence_token session_token_ref agent_id
    actual_host_worker_id worker_session_id worker_transcript_ref
    worker_transcript_path filer_principal host_session_id host_startup_id head_commit
    read_receipt_hash read_receipt_event_id now_iso actual_cwd actual_git_root
    harness_type""".split()
)
_STARTUP_REQUIRED_FIELDS = tuple(
    """project_id runtime_context_id task_id parent_task_id session_token fence_token
    session_token_ref agent_id actual_host_worker_id worker_session_id filer_principal
    host_session_id host_startup_id head_commit read_receipt_hash
    read_receipt_event_id""".split()
)
_GRAPH_CONTINUATION_TOOLS = frozenset(
    {"function_index", "function_callers", "function_callees"}
)
_GRAPH_ROUTE_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
)
_GRAPH_SCOPE_FIELDS = (
    "project_id",
    "backlog_id",
    "runtime_context_id",
    "task_id",
    "parent_task_id",
    "target_project_root",
    "project_root",
    "repo_root",
    "query_source",
    "query_purpose",
)
_GRAPH_QUERY_TOOL_FIELDS = frozenset(
    """actor args backlog_id commit_sha fence_token parent_task_id project_id
    project_root prompt_contract_hash prompt_contract_id query_purpose query_source
    repo_root route_context_hash route_id route_identity route_token_ref
    runtime_context_id session_token session_token_ref snapshot_id
    target_project_root task_id tool visible_injection_manifest_hash
    worker_role""".split()
)
_GRAPH_QUERY_BODY_PATHS = (
    ("actionable_payloads", "graph_query_submission", "copy_safe_body"),
    ("actionable_payloads", "graph_query_facade_payload_skeleton", "copy_safe_body"),
    ("corrected_request_shapes", "graph_query_body"),
    ("target_project_root_projection", "corrected_request_shapes", "graph_query_body"),
    ("details", "actionable_payloads", "graph_query_submission", "copy_safe_body"),
    (
        "details",
        "actionable_payloads",
        "graph_query_facade_payload_skeleton",
        "copy_safe_body",
    ),
    ("details", "corrected_request_shapes", "graph_query_body"),
    (
        "details",
        "target_project_root_projection",
        "corrected_request_shapes",
        "graph_query_body",
    ),
    (
        "details",
        "diagnostics",
        "target_project_root_projection",
        "corrected_request_shapes",
        "graph_query_body",
    ),
    (
        "details",
        "compatibility",
        "corrected_request_shapes",
        "graph_query_body",
    ),
)
_GRAPH_HOST_ENVELOPE_PATHS = (
    ("host_envelope",),
    ("worker_host_envelope",),
    ("details", "host_envelope"),
    ("details", "worker_host_envelope"),
    ("data", "host_envelope"),
    ("data", "worker_host_envelope"),
)

_ADMISSION_FIELDS = frozenset(
    {
        "authority_selectors",
    }
)
_AUTHORITY_FIELDS = frozenset(
    {
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "worker_id",
        "worker_slot_id",
        "observer_command_id",
        "role",
        "profile_id",
        "principal_id",
        "expected_execution_state_revision",
        "expected_execution_state_hash",
        "expected_dispatch_identity_hash",
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "route_token_ref",
        "visible_injection_manifest_hash",
        "harness",
        "provider",
        "model",
        "runtime_id",
        "endpoint_id",
        "launcher_id",
        "backend_mode",
    }
)
_REQUIRED_AUTHORITY_FIELDS = (
    "project_id",
    "backlog_id",
    "contract_execution_id",
    "runtime_context_id",
    "task_id",
    "worker_id",
    "worker_slot_id",
    "observer_command_id",
    "role",
    "profile_id",
    "principal_id",
    "expected_execution_state_hash",
    "expected_dispatch_identity_hash",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
    "backend_mode",
)


class GuidedRuntimeDispatchError(RuntimeError):
    """A governed service dispatch failed without invoking a local fallback."""

    def __init__(self, message: str, *, status: str = "blocked") -> None:
        super().__init__(message)
        self.status = status


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GuidedRuntimeDispatchError(
            "guided runtime admission requires {}".format(field_name)
        )
    return dict(value)


def _json_round_trip(value: Any, field_name: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise GuidedRuntimeDispatchError(
            "{} must be JSON compatible".format(field_name),
            status="invalid_host_orchestration",
        ) from exc


def _first_deep_value(
    value: Any, field_names: tuple[str, ...], *, want_mapping: bool
) -> Any:
    if isinstance(value, Mapping):
        for field_name in field_names:
            candidate = value.get(field_name)
            if want_mapping and isinstance(candidate, Mapping):
                return dict(candidate)
            if not want_mapping and isinstance(candidate, (str, int)):
                text = _text(candidate)
                if text and not _PLACEHOLDER.search(text):
                    return text
        for nested in value.values():
            found = _first_deep_value(
                nested, field_names, want_mapping=want_mapping
            )
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _first_deep_value(
                nested, field_names, want_mapping=want_mapping
            )
            if found:
                return found
    return {} if want_mapping else ""


def _first_named_mapping(value: Any, field_name: str) -> dict[str, Any]:
    return _first_deep_value(value, (field_name,), want_mapping=True)


def _first_deep_text(value: Any, *field_names: str) -> str:
    return _first_deep_value(value, field_names, want_mapping=False)


def _submission_body(
    guide: Mapping[str, Any],
    submission_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    submission = _first_named_mapping(guide, submission_name)
    if not submission:
        raise GuidedRuntimeDispatchError(
            "runtime context worker guide is missing {}".format(submission_name),
            status="invalid_host_orchestration",
        )
    body = submission.get("copy_safe_body")
    if not isinstance(body, Mapping):
        body = submission.get("body")
    if not isinstance(body, Mapping):
        raise GuidedRuntimeDispatchError(
            "{} is missing a copy-safe body".format(submission_name),
            status="invalid_host_orchestration",
        )
    return submission, _json_round_trip(body, submission_name)


def _stable_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _host_runtime_values(
    guide: Mapping[str, Any],
    host_identity: Mapping[str, Any],
    *,
    project_id: str,
    reason: str,
    now_iso: str,
    read_receipt_hash: str,
) -> dict[str, Any]:
    host_input = {
        str(key): value
        for key, value in host_identity.items()
        if str(key) in _HOST_REPLACEMENT_FIELDS_BY_SOURCE["host_identity"]
    }
    supplied = _json_round_trip(host_input, "host_identity")
    worker_session = _text(
        supplied.get("worker_session_id") or supplied.get("host_session_id")
    )
    if not worker_session:
        raise GuidedRuntimeDispatchError(
            "runtime context host orchestration requires worker_session_id",
            status="invalid_host_orchestration",
        )
    host_session = _text(supplied.get("host_session_id")) or worker_session
    timestamp = _text(now_iso or supplied.get("now_iso")) or datetime.now(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")
    startup = _text(supplied.get("host_startup_id")) or "host-startup-{}".format(
        _stable_json_hash(
            {"worker_session_id": worker_session, "now_iso": timestamp}
        ).split(":", 1)[1][:20]
    )
    project = _text(project_id or supplied.get("project_id")) or _first_deep_text(
        guide, "project_id"
    )
    if not project:
        raise GuidedRuntimeDispatchError(
            "runtime context host orchestration requires project_id",
            status="invalid_host_orchestration",
        )
    receipt_hash = _text(
        read_receipt_hash or supplied.get("read_receipt_hash")
    ) or _stable_json_hash(
        {
            "project_id": project,
            "runtime_context_id": _first_deep_text(guide, "runtime_context_id"),
            "task_id": _first_deep_text(guide, "task_id"),
            "worker_session_id": worker_session,
            "host_session_id": host_session,
            "host_startup_id": startup,
            "acknowledged_at": timestamp,
        }
    )
    transcript_path = _text(supplied.get("worker_transcript_path"))
    transcript_ref = _text(supplied.get("worker_transcript_ref"))
    if not (transcript_ref or transcript_path):
        transcript_ref = "codex:{}".format(worker_session)
    values = {
        **supplied,
        "project_id": project,
        "reason": _text(reason) or "host adapter needs first worker auth env",
        "worker_session_id": worker_session,
        "host_session_id": host_session,
        "host_startup_id": startup,
        "worker_transcript_ref": transcript_ref,
        "worker_transcript_path": transcript_path,
        "filer_principal": _text(supplied.get("filer_principal")) or worker_session,
        "actor_session_principal": _text(supplied.get("actor_session_principal"))
        or worker_session,
        "acknowledged_at": timestamp,
        "now_iso": timestamp,
        "read_receipt_hash": receipt_hash,
        "receipt_hash": receipt_hash,
        "launch_text_hash": _text(supplied.get("launch_text_hash")) or receipt_hash,
        "head_commit": _text(supplied.get("head_commit")),
        "actual_cwd": _text(supplied.get("actual_cwd"))
        or _first_deep_text(guide, "target_project_root"),
        "actual_git_root": _text(supplied.get("actual_git_root"))
        or _first_deep_text(guide, "target_project_root"),
        "harness_type": _text(supplied.get("harness_type")) or "codex",
    }
    for field_name in _HOST_REPLACEMENT_FIELDS_BY_SOURCE["worker_guide"]:
        guide_value = _first_deep_text(guide, field_name)
        if field_name in {"agent_id", "actual_host_worker_id", "worker_id", "worker_slot_id"}:
            # Allocation identity is authoritative; host identity belongs in
            # worker_session_id/host_session_id.
            values[field_name] = guide_value or values.get(field_name, "")
        else:
            values[field_name] = values.get(field_name) or guide_value
    return values


def _validate_placeholder_contract(
    templates: tuple[tuple[str, Mapping[str, Any], frozenset[str]], ...],
) -> None:
    undocumented: list[str] = []

    def walk(value: Any, path: str, field_name: str = "") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                name = str(key)
                walk(nested, "{}.{}".format(path, name), name)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, "{}[{}]".format(path, index), field_name)
        elif (
            isinstance(value, str)
            and _PLACEHOLDER.search(value)
            and field_name not in _HOST_REPLACEMENT_FIELDS
        ):
            undocumented.append(path)

    for tool_name, template, allowed_fields in templates:
        walk(
            {
                str(key): value
                for key, value in template.items()
                if str(key) in allowed_fields
            },
            tool_name,
        )
    if undocumented:
        field_names = sorted({path.rsplit(".", 1)[-1] for path in undocumented})
        raise GuidedRuntimeDispatchError(
            "runtime context host tool body is incomplete: unresolved {}; "
            "undocumented replacement source(s): {}".format(
                ", ".join(field_names), ", ".join(sorted(undocumented))
            ),
            status="invalid_host_orchestration",
        )


def _validated_tool_body(
    template: Mapping[str, Any],
    *,
    allowed_fields: frozenset[str],
    replacements: Mapping[str, Any],
    force_fields: frozenset[str],
    required_fields: tuple[str, ...],
) -> dict[str, Any]:
    def realize(value: Any, path: str = "") -> tuple[Any, list[str]]:
        if isinstance(value, Mapping):
            result, unresolved = {}, []
            for key, nested in value.items():
                field = str(key)
                child_path = "{}.{}".format(path, field) if path else field
                replacement = replacements.get(field)
                if field == "worker_transcript_path" and not _text(replacement):
                    continue
                if replacement not in (None, "") and (
                    field in force_fields
                    or nested in (None, "")
                    or isinstance(nested, str) and _PLACEHOLDER.search(nested)
                ):
                    result[field] = _json_round_trip(replacement, "host realization")
                    continue
                result[field], nested_unresolved = realize(nested, child_path)
                unresolved.extend(nested_unresolved)
            return result, unresolved
        if isinstance(value, list):
            result, unresolved = [], []
            for index, nested in enumerate(value):
                child, nested_unresolved = realize(nested, "{}[{}]".format(path, index))
                result.append(child)
                unresolved.extend(nested_unresolved)
            return result, unresolved
        if isinstance(value, str) and _PLACEHOLDER.search(value):
            return value, [path or "<root>"]
        return value, []

    filtered = {str(key): value for key, value in template.items() if str(key) in allowed_fields}
    for field_name in force_fields:
        replacement = replacements.get(field_name)
        if replacement not in (None, "") and field_name in allowed_fields:
            filtered[field_name] = replacement
    realized, placeholders = realize(filtered)
    missing = [field for field in required_fields if not _text(realized.get(field))]
    if missing or placeholders:
        details = (["missing {}".format(", ".join(missing))] if missing else [])
        if placeholders:
            details.append("unresolved {}".format(", ".join(placeholders)))
        raise GuidedRuntimeDispatchError(
            "runtime context host tool body is incomplete: {}".format(
                "; ".join(details)
            ),
            status="invalid_host_orchestration",
        )
    return realized


def _application_succeeded(value: Mapping[str, Any]) -> bool:
    if value.get("isError") is True or value.get("ok") is False:
        return False
    if value.get("error") not in (None, "", {}, []):
        return False
    if value.get("ok") is True:
        return True
    if value.get("code") not in (None, "", 0, "0"):
        return False
    return _text(value.get("status")).lower() not in {
        "blocked",
        "error",
        "failed",
        "invalid",
        "invalid_request",
        "rejected",
    }


def _public_server_failure(
    tool_name: str,
    response: Mapping[str, Any],
    *,
    raw_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    public = _json_round_trip(response, "server response")
    scrub_host_secret_values(public, raw_values=raw_values)
    result = dict(public)
    result["ok"] = False
    result.setdefault("status", "host_tool_rejected")
    result.update(
        {
            "host_orchestration_schema_version": (
                RUNTIME_CONTEXT_HOST_ORCHESTRATION_SCHEMA_VERSION
            ),
            "failed_tool": tool_name,
            "server_response": public,
            **_HOST_PRIVACY_FLAGS,
        }
    )
    return result


def _invoke_host_tool(
    tool_caller: Callable[[str, Mapping[str, Any]], Any],
    tool_name: str,
    body: dict[str, Any],
    *,
    response_status: str,
    request_bodies: list[dict[str, Any]],
    raw_results: list[Any],
    raw_values: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any] | None, tuple[str, ...]]:
    request_bodies.append(body)
    try:
        raw = tool_caller(tool_name, body)
    except Exception as exc:
        return {}, {
            "schema_version": RUNTIME_CONTEXT_HOST_ORCHESTRATION_SCHEMA_VERSION,
            "ok": False,
            "status": "host_tool_invocation_failed",
            "failed_tool": tool_name,
            "error": {
                "type": type(exc).__name__,
                "message": "{} tool invocation failed".format(response_status),
            },
            **_HOST_PRIVACY_FLAGS,
        }, raw_values
    raw_results.append(raw)
    try:
        response = unwrap_mcp_application_response(raw)
    except ServiceError as exc:
        raise GuidedRuntimeDispatchError(
            "{} response could not be decoded".format(response_status),
            status="{}_response_invalid".format(response_status.replace(" ", "_")),
        ) from exc
    envelope = _first_named_mapping(response, "host_envelope") or _first_named_mapping(
        response, "worker_host_envelope"
    )
    environment = envelope.get("env")
    environment = environment if isinstance(environment, Mapping) else {}
    discovered = (
        _text(environment.get("AMING_WORKER_SESSION_TOKEN") or response.get("session_token")),
        _text(environment.get("AMING_WORKER_FENCE_TOKEN") or response.get("fence_token")),
    )
    raw_values = tuple(dict.fromkeys((*raw_values, *(item for item in discovered if item))))
    if not _application_succeeded(response):
        return response, _public_server_failure(
            tool_name, response, raw_values=raw_values
        ), raw_values
    return response, None, raw_values


def orchestrate_runtime_context_host_startup(
    *,
    worker_guide: Mapping[str, Any],
    tool_caller: Callable[[str, Mapping[str, Any]], Any],
    host_identity: Mapping[str, Any],
    project_id: str = "",
    reason: str = "",
    now_iso: str = "",
    read_receipt_hash: str = "",
) -> dict[str, Any]:
    """Run initial-join -> read-receipt -> startup in one host invocation.

    Raw worker credentials exist only in this stack frame and in the two
    protected tool request bodies.  All bodies and results are scrubbed on
    success, rejection, validation failure, and transport failure.
    """

    try:
        guide = unwrap_mcp_application_response(worker_guide)
    except ServiceError as exc:
        raise GuidedRuntimeDispatchError(
            "runtime context worker guide could not be decoded",
            status="invalid_host_orchestration",
        ) from exc
    initial_submission, initial_template = _submission_body(
        guide, "session_token_initial_join_submission"
    )
    receipt_submission, receipt_template = _submission_body(
        guide, "read_receipt_facade_payload_skeleton"
    )
    startup_submission, startup_template = _submission_body(
        guide, "startup_facade_payload_skeleton"
    )
    _validate_placeholder_contract(
        (
            ("initial_join", initial_template, _INITIAL_JOIN_TOOL_FIELDS),
            ("read_receipt", receipt_template, _READ_RECEIPT_TOOL_FIELDS),
            ("startup", startup_template, _STARTUP_TOOL_FIELDS),
        )
    )
    values = _host_runtime_values(
        guide,
        _mapping(host_identity, "host runtime identity"),
        project_id=project_id,
        reason=reason,
        now_iso=now_iso,
        read_receipt_hash=read_receipt_hash,
    )
    initial_body = _validated_tool_body(
        initial_template,
        allowed_fields=_INITIAL_JOIN_TOOL_FIELDS,
        replacements=values,
        force_fields=_INITIAL_FORCE_FIELDS,
        required_fields=_INITIAL_REQUIRED_FIELDS,
    )
    initial_tool = _text(
        initial_submission.get("mcp_tool") or initial_submission.get("tool")
    ) or "runtime_context_session_token_initial_join"
    receipt_tool = _text(
        receipt_submission.get("mcp_tool") or receipt_submission.get("tool")
    ) or "runtime_context_read_receipt"
    startup_tool = _text(
        startup_submission.get("mcp_tool")
        or startup_submission.get("tool")
        or startup_submission.get("legacy_tool")
    ) or "parallel_branch_startup"

    raw_results: list[Any] = []
    request_bodies: list[dict[str, Any]] = []
    raw_values: tuple[str, ...] = ()
    try:
        initial_response, failure, raw_values = _invoke_host_tool(
            tool_caller,
            initial_tool,
            initial_body,
            response_status="initial join",
            request_bodies=request_bodies,
            raw_results=raw_results,
        )
        if failure:
            return failure

        host_envelope = _first_named_mapping(
            initial_response, "host_envelope"
        ) or _first_named_mapping(initial_response, "worker_host_envelope")
        environment = host_envelope.get("env")
        if not isinstance(environment, Mapping):
            environment = {}
        session_token = _text(
            environment.get("AMING_WORKER_SESSION_TOKEN")
            or initial_response.get("session_token")
        )
        fence_token = _text(
            environment.get("AMING_WORKER_FENCE_TOKEN")
            or initial_response.get("fence_token")
        )
        raw_values = tuple(value for value in (session_token, fence_token) if value)
        joined_session_token_ref = _first_deep_text(
            initial_response,
            "session_token_ref",
            "worker_session_token_ref",
            "safe_session_token_ref",
        )
        if not session_token or not fence_token or not joined_session_token_ref:
            public = _json_round_trip(initial_response, "initial join response")
            scrub_host_secret_values(public, raw_values=raw_values)
            return {
                "schema_version": RUNTIME_CONTEXT_HOST_ORCHESTRATION_SCHEMA_VERSION,
                "ok": False,
                "status": "initial_join_response_invalid",
                "failed_tool": initial_tool,
                "error": {
                    "code": "initial_join_worker_auth_or_safe_ref_missing",
                    "message": (
                        "successful initial join omitted required host auth or "
                        "copy-safe session reference"
                    ),
                },
                "server_response": public,
                **_HOST_PRIVACY_FLAGS,
            }

        protected_values = {
            **values,
            "session_token": session_token,
            "fence_token": fence_token,
            # The accepted join rotates allocation auth.  Its safe ref is the
            # only authoritative ref for both protected worker writes below.
            "session_token_ref": joined_session_token_ref,
        }
        receipt_body = _validated_tool_body(
            receipt_template,
            allowed_fields=_READ_RECEIPT_TOOL_FIELDS,
            replacements=protected_values,
            force_fields=_RECEIPT_FORCE_FIELDS,
            required_fields=_RECEIPT_REQUIRED_FIELDS,
        )
        receipt_response, failure, raw_values = _invoke_host_tool(
            tool_caller,
            receipt_tool,
            receipt_body,
            response_status="read receipt",
            request_bodies=request_bodies,
            raw_results=raw_results,
            raw_values=raw_values,
        )
        if failure:
            return failure
        accepted_receipt_hash = _first_deep_text(
            receipt_response, "read_receipt_hash", "receipt_hash"
        ) or _text(protected_values.get("read_receipt_hash"))
        accepted_receipt_event_id = _first_deep_text(
            receipt_response,
            "read_receipt_event_id",
            "read_receipt_event_ref",
            "event_id",
        )
        startup_values = {
            **protected_values,
            "read_receipt_hash": accepted_receipt_hash,
            "receipt_hash": accepted_receipt_hash,
            "read_receipt_event_id": accepted_receipt_event_id,
        }
        startup_body = _validated_tool_body(
            startup_template,
            allowed_fields=_STARTUP_TOOL_FIELDS,
            replacements=startup_values,
            force_fields=_STARTUP_FORCE_FIELDS,
            required_fields=_STARTUP_REQUIRED_FIELDS,
        )
        if not (
            _text(startup_body.get("worker_transcript_ref"))
            or _text(startup_body.get("worker_transcript_path"))
        ):
            raise GuidedRuntimeDispatchError(
                "runtime context startup requires a transcript ref or path",
                status="invalid_host_orchestration",
            )
        startup_response, failure, raw_values = _invoke_host_tool(
            tool_caller,
            startup_tool,
            startup_body,
            response_status="startup",
            request_bodies=request_bodies,
            raw_results=raw_results,
            raw_values=raw_values,
        )
        if failure:
            return failure

        public_join = _json_round_trip(initial_response, "initial join response")
        public_receipt = _json_round_trip(receipt_response, "read receipt response")
        public_startup = _json_round_trip(startup_response, "startup response")
        for public in (public_join, public_receipt, public_startup):
            scrub_host_secret_values(public, raw_values=raw_values)
        return {
            "schema_version": RUNTIME_CONTEXT_HOST_ORCHESTRATION_SCHEMA_VERSION,
            "ok": True,
            "status": "started",
            "sequence": [initial_tool, receipt_tool, startup_tool],
            "session_token_ref": joined_session_token_ref,
            "host_session_id": _text(values.get("host_session_id")),
            "host_startup_id": _text(values.get("host_startup_id")),
            "initial_join": public_join,
            "read_receipt": public_receipt,
            "startup": public_startup,
            "uninterrupted_same_invocation": True,
            **_HOST_PRIVACY_FLAGS,
        }
    finally:
        for request_body in request_bodies:
            scrub_host_secret_values(request_body, raw_values=raw_values)
        for raw_result in raw_results:
            scrub_host_secret_values(raw_result, raw_values=raw_values)


def _authority_selectors(
    value: Any,
    *,
    project_id: str,
    backlog_id: str,
) -> dict[str, Any]:
    selectors = _mapping(value, "ContractRuntime authority selectors")
    unsupported = sorted(set(selectors) - _AUTHORITY_FIELDS)
    if unsupported:
        raise GuidedRuntimeDispatchError(
            "guided runtime selectors contain unsupported authority fields"
        )
    missing = [
        field_name
        for field_name in _REQUIRED_AUTHORITY_FIELDS
        if not _text(selectors.get(field_name))
    ]
    if missing:
        raise GuidedRuntimeDispatchError(
            "guided runtime selectors are incomplete: {}".format(
                ", ".join(missing)
            )
        )
    try:
        revision = int(selectors.get("expected_execution_state_revision") or 0)
    except (TypeError, ValueError) as exc:
        raise GuidedRuntimeDispatchError(
            "guided runtime selector revision is invalid"
        ) from exc
    if revision <= 0:
        raise GuidedRuntimeDispatchError(
            "guided runtime selectors require current authority coordinates"
        )
    if _text(selectors.get("project_id")) != _text(project_id):
        raise GuidedRuntimeDispatchError(
            "guided runtime selector project does not match the observer request"
        )
    if _text(selectors.get("backlog_id")) != _text(backlog_id):
        raise GuidedRuntimeDispatchError(
            "guided runtime selector backlog does not match the observer request"
        )
    if _text(selectors.get("principal_id")) != _text(selectors.get("worker_id")):
        raise GuidedRuntimeDispatchError(
            "guided runtime principal must match canonical dispatch identity"
        )
    selectors["expected_execution_state_revision"] = revision
    return selectors


def _transient_host_envelope(value: Any) -> dict[str, Any]:
    envelope = _mapping(value, "one transient worker host envelope")
    if set(envelope) != {"env"}:
        raise GuidedRuntimeDispatchError(
            "guided runtime accepts only a transient worker auth envelope"
        )
    environment = _mapping(envelope.get("env"), "worker host envelope auth")
    if set(environment) != set(WORKER_AUTH_ENV_KEYS):
        raise GuidedRuntimeDispatchError(
            "guided runtime worker host envelope has invalid auth fields"
        )
    normalized = {key: environment.get(key) for key in WORKER_AUTH_ENV_KEYS}
    if any(
        not isinstance(value, str) or not value or "\x00" in value
        for value in normalized.values()
    ):
        raise GuidedRuntimeDispatchError(
            "guided runtime worker host envelope is incomplete"
        )
    return {"env": normalized}


def request_guided_runtime(
    *,
    admission: Mapping[str, Any],
    project_id: str,
    backlog_id: str,
    transient_host_envelope: Mapping[str, Any],
    state_dir: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Submit canonical ticket selectors for daemon-owned run admission."""

    admission_value = _mapping(admission, "guided service admission")
    if set(admission_value) - _ADMISSION_FIELDS:
        raise GuidedRuntimeDispatchError(
            "guided service admission contains unsupported fields"
        )
    selectors = _authority_selectors(
        admission_value.get("authority_selectors"),
        project_id=project_id,
        backlog_id=backlog_id,
    )
    envelope: dict[str, Any] = {}
    try:
        envelope = _transient_host_envelope(transient_host_envelope)
        try:
            response = request_service(
                ServicePaths.from_state_dir(state_dir or None),
                "start_host_envelope_run",
                payload={
                    "authority_selectors": selectors,
                    "host_envelope": envelope,
                },
                timeout_seconds=timeout_seconds,
            )
        except ServiceUnavailableError as exc:
            raise GuidedRuntimeDispatchError(
                "CLI Agent Service is unavailable",
                status="unavailable",
            ) from exc
        except ServiceError as exc:
            raise GuidedRuntimeDispatchError(
                "CLI Agent Service rejected the governed run: {}".format(exc),
                status="rejected",
            ) from exc
    finally:
        scrub_host_envelope_payload(envelope)
        scrub_host_envelope_payload(transient_host_envelope)
    if response.get("ok") is not True or response.get("status") != "started":
        raise GuidedRuntimeDispatchError(
            _text(response.get("error"))
            or "CLI Agent Service rejected the governed run",
            status=_text(response.get("status")) or "rejected",
        )
    response_identity = {
        "role": response.get("role"),
        "profile_id": response.get("profile_id"),
        "principal_id": response.get("principal_id"),
        "runtime_context_id": response.get("runtime_context_id"),
        "task_id": response.get("task_id"),
        "contract_execution_id": response.get("contract_execution_id"),
    }
    mismatches = [
        field_name
        for field_name, actual in response_identity.items()
        if _text(actual) != _text(selectors.get(field_name))
    ]
    if not _text(response.get("run_id")):
        mismatches.append("run_id")
    for field_name in (
        "direct_invocation_fallback",
        "caller_run_accepted",
        "caller_prompt_accepted",
        "caller_environment_accepted",
    ):
        if response.get(field_name) is not False:
            mismatches.append(field_name)
    for field_name in (
        "transient_host_envelope_required",
        "transient_host_envelope_accepted",
        "transient_host_envelope_consumed",
        "provider_output_suppressed",
    ):
        if response.get(field_name) is not True:
            mismatches.append(field_name)
    for field_name in (
        "transient_host_envelope_persisted",
        "host_envelope_run_authority",
        "raw_session_token_persisted",
        "raw_fence_token_persisted",
        "raw_provider_output_persisted",
    ):
        if response.get(field_name) is not False:
            mismatches.append(field_name)
    if mismatches:
        raise GuidedRuntimeDispatchError(
            "CLI Agent Service returned mismatched governed run identity: {}".format(
                ", ".join(sorted(set(mismatches)))
            ),
            status="rejected",
        )
    canonical_run_id = _text(response.get("run_id"))
    official_runtime_startup_identity = {
        "actual_host_worker_id": canonical_run_id,
        "worker_session_id": canonical_run_id,
        "filer_principal": canonical_run_id,
        "worker_transcript_ref": "codex:{}".format(canonical_run_id),
    }
    return {
        "schema_version": GUIDED_RUNTIME_DISPATCH_SCHEMA_VERSION,
        "ok": True,
        "status": "started",
        "operation": "start_host_envelope_run",
        "run_id": canonical_run_id,
        "official_runtime_startup_identity": official_runtime_startup_identity,
        "role": _text(response.get("role")),
        "profile_id": _text(response.get("profile_id")),
        "principal_id": _text(response.get("principal_id")),
        "runtime_context_id": _text(response.get("runtime_context_id")),
        "task_id": _text(response.get("task_id")),
        "contract_execution_id": _text(response.get("contract_execution_id")),
        "authority_selectors": dict(selectors),
        "service_response": dict(response),
        "direct_invocation_fallback": False,
        "raw_session_token_exposed": False,
        "raw_fence_token_exposed": False,
        "caller_run_accepted": False,
        "caller_prompt_accepted": False,
        "caller_environment_accepted": False,
        "transient_host_envelope_required": True,
        "transient_host_envelope_accepted": True,
        "transient_host_envelope_consumed": True,
        "transient_host_envelope_persisted": False,
        "host_envelope_run_authority": False,
        "raw_session_token_persisted": False,
        "raw_fence_token_persisted": False,
        "raw_provider_output_persisted": False,
        "provider_output_suppressed": True,
        "governance_authority": False,
        "operational_dispatch_only": True,
    }


def request_qa_execution_ticket_runtime(
    *,
    authority_selectors: Mapping[str, Any],
    qa_session_token: str,
    state_dir: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one QA ticket without accepting caller launch material."""
    selectors = _mapping(authority_selectors, "QA ContractRuntime selectors")
    unsupported = sorted(set(selectors) - _AUTHORITY_FIELDS)
    if unsupported:
        raise GuidedRuntimeDispatchError(
            "QA guided runtime selectors contain unsupported authority fields"
        )
    required = tuple(
        field for field in _REQUIRED_AUTHORITY_FIELDS if field != "profile_id"
    )
    missing = [field for field in required if not _text(selectors.get(field))]
    if missing or _text(selectors.get("role")).lower() != "qa":
        raise GuidedRuntimeDispatchError(
            "QA guided runtime requires complete QA authority selectors"
        )
    token = _text(qa_session_token)
    if not token:
        raise GuidedRuntimeDispatchError("QA guided runtime requires a QA session token")
    payload = {
        "authority_selectors": dict(selectors),
        "qa_session_token": token,
    }
    try:
        response = request_service(
            ServicePaths.from_state_dir(state_dir or None),
            "start_qa_execution_ticket_run",
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
    except (ServiceUnavailableError, ServiceError) as exc:
        raise GuidedRuntimeDispatchError(
            "CLI Agent Service rejected the QA execution ticket",
            status="rejected",
        ) from exc
    finally:
        payload["qa_session_token"] = ""
        token = ""
    if response.get("ok") is not True or response.get("status") != "started":
        raise GuidedRuntimeDispatchError(
            _text(response.get("error")) or "QA execution ticket was rejected",
            status=_text(response.get("status")) or "rejected",
        )
    if (
        _text(response.get("role")).lower() != "qa"
        or not _text(response.get("run_id"))
        or response.get("caller_run_accepted") is not False
        or response.get("caller_prompt_accepted") is not False
        or response.get("caller_environment_accepted") is not False
        or response.get("transient_qa_session_token_accepted") is not True
        or response.get("transient_qa_session_token_persisted") is not False
    ):
        raise GuidedRuntimeDispatchError(
            "CLI Agent Service returned invalid QA admission evidence",
            status="rejected",
        )
    return {
        "schema_version": "cli_agent_service.qa_execution_ticket_dispatch.v1",
        "ok": True,
        "status": "started",
        "run_id": _text(response.get("run_id")),
        "role": "qa",
        "profile_id": _text(response.get("profile_id")),
        "service_response": dict(response),
        "caller_run_accepted": False,
        "caller_prompt_accepted": False,
        "caller_environment_accepted": False,
        "transient_qa_session_token_persisted": False,
    }


def request_contract_runtime_observer(
    *,
    current_state: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
    profile_requirements: Mapping[str, Any],
    transient_host_envelope: Mapping[str, Any],
    state_dir: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Resolve and dispatch the supported source-backed L2 observer path."""
    from agent.governance.contract_state_runtime import (
        resolve_cli_agent_observer_admission,
    )

    admission = resolve_cli_agent_observer_admission(
        current_state,
        runtime_identity=runtime_identity,
        profile_requirements=profile_requirements,
    )
    selectors = admission["authority_selectors"]
    return request_guided_runtime(
        admission={"authority_selectors": selectors},
        project_id=selectors["project_id"],
        backlog_id=selectors["backlog_id"],
        transient_host_envelope=transient_host_envelope,
        state_dir=state_dir,
        timeout_seconds=timeout_seconds,
    )


def _graph_continuation_error(message: str) -> GuidedRuntimeDispatchError:
    return GuidedRuntimeDispatchError(
        message,
        status="invalid_graph_continuation",
    )


def _graph_route_identity(body: Mapping[str, Any]) -> dict[str, str]:
    nested = body.get("route_identity")
    nested = dict(nested) if isinstance(nested, Mapping) else {}
    route: dict[str, str] = {}
    for field_name in _GRAPH_ROUTE_FIELDS:
        top_level = _text(body.get(field_name))
        nested_value = _text(nested.get(field_name))
        values = {value for value in (top_level, nested_value) if value}
        if len(values) > 1:
            raise _graph_continuation_error(
                "authenticated guide contains conflicting {}".format(field_name)
            )
        value = next(iter(values), "")
        if not value or _PLACEHOLDER.search(value):
            raise _graph_continuation_error(
                "authenticated guide is missing exact {}".format(field_name)
            )
        route[field_name] = value
    return route


def _graph_host_auth(
    host_auth_response: Mapping[str, Any],
) -> tuple[dict[str, Any], str, str, str]:
    try:
        application = unwrap_mcp_application_response(host_auth_response)
        envelopes = mcp_application_mapping_blocks(
            application,
            paths=_GRAPH_HOST_ENVELOPE_PATHS,
        )
    except ServiceError as exc:
        raise _graph_continuation_error(
            "worker host envelope could not be decoded"
        ) from exc
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    refs: set[str] = set()
    for envelope in envelopes:
        environment = envelope.get("env")
        environment = dict(environment) if isinstance(environment, Mapping) else {}
        session_token = _text(
            environment.get(WORKER_AUTH_ENV_KEYS[0])
            or envelope.get("session_token")
        )
        fence_token = _text(
            environment.get(WORKER_AUTH_ENV_KEYS[1])
            or envelope.get("fence_token")
        )
        if session_token and fence_token:
            candidates[(session_token, fence_token)] = envelope
        for field_name in (
            "session_token_ref",
            "worker_session_token_ref",
            "safe_session_token_ref",
        ):
            value = _text(envelope.get(field_name))
            if value:
                refs.add(value)
    for field_name in (
        "session_token_ref",
        "worker_session_token_ref",
        "safe_session_token_ref",
    ):
        value = _text(application.get(field_name))
        if value:
            refs.add(value)
    if len(candidates) != 1 or len(refs) != 1:
        raise _graph_continuation_error(
            "worker host envelope auth or rotated safe ref is ambiguous"
        )
    session_token, fence_token = next(iter(candidates))
    if _PLACEHOLDER.search(session_token) or _PLACEHOLDER.search(fence_token):
        raise _graph_continuation_error(
            "worker host envelope contains placeholder auth"
        )
    return application, session_token, fence_token, next(iter(refs))


def orchestrate_runtime_context_graph_continuation(
    *,
    worker_guide: Mapping[str, Any],
    host_auth_response: Mapping[str, Any],
    tool_caller: Callable[[str, Mapping[str, Any]], Any],
    expected_scope: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Continue exact-symbol graph work from authenticated guide authority."""

    try:
        guide = unwrap_mcp_application_response(worker_guide)
    except ServiceError as exc:
        raise _graph_continuation_error(
            "authenticated worker guide could not be decoded"
        ) from exc
    scope = _mapping(expected_scope, "expected graph scope")
    candidates = mcp_application_mapping_blocks(
        guide,
        paths=_GRAPH_QUERY_BODY_PATHS,
    )
    if not candidates:
        raise _graph_continuation_error(
            "authenticated guide has no canonical graph query body"
        )
    unique = {
        json.dumps(candidate, sort_keys=True, separators=(",", ":")): candidate
        for candidate in candidates
    }
    if len(unique) != 1:
        raise _graph_continuation_error(
            "authenticated guide graph query body is ambiguous"
        )
    template = next(iter(unique.values()))
    for field_name in _GRAPH_SCOPE_FIELDS:
        expected = _text(scope.get(field_name))
        actual = _text(template.get(field_name))
        if (
            not expected
            or not actual
            or _PLACEHOLDER.search(actual)
            or actual != expected
        ):
            raise _graph_continuation_error(
                "authenticated guide graph scope conflicts at {}".format(field_name)
            )
    route = _graph_route_identity(template)
    expected_route = scope.get("route_identity")
    expected_route = (
        dict(expected_route) if isinstance(expected_route, Mapping) else scope
    )
    for field_name, actual in route.items():
        expected = _text(expected_route.get(field_name))
        if not expected or expected != actual:
            raise _graph_continuation_error(
                "authenticated guide route conflicts at {}".format(field_name)
            )
    requested: list[dict[str, Any]] = []
    for query in queries:
        item = _mapping(query, "graph query")
        tool_name = _text(item.get("tool"))
        args = item.get("args")
        symbol = _text(args.get("query")) if isinstance(args, Mapping) else ""
        if (
            tool_name not in _GRAPH_CONTINUATION_TOOLS
            or set(item) != {"tool", "args"}
            or not isinstance(args, Mapping)
            or set(args) != {"query"}
            or not symbol
            or _PLACEHOLDER.search(symbol)
        ):
            raise _graph_continuation_error(
                "graph continuation accepts exact-symbol function queries only"
            )
        requested.append({"tool": tool_name, "args": {"query": symbol}})
    if not requested:
        raise _graph_continuation_error(
            "graph continuation requires at least one exact-symbol query"
        )

    application: dict[str, Any] = {}
    raw_results: list[Any] = []
    request_bodies: list[dict[str, Any]] = []
    raw_values: tuple[str, ...] = ()
    try:
        application, session_token, fence_token, session_token_ref = (
            _graph_host_auth(host_auth_response)
        )
        raw_values = (session_token, fence_token)
        guide_ref = _text(template.get("session_token_ref"))
        if guide_ref and guide_ref != session_token_ref:
            raise _graph_continuation_error(
                "authenticated guide safe ref is stale"
            )
        traces: list[str] = []
        summaries: list[dict[str, str]] = []
        for query in requested:
            body = {
                str(key): _json_round_trip(value, "graph query body")
                for key, value in template.items()
                if str(key) in _GRAPH_QUERY_TOOL_FIELDS
            }
            body.update(query)
            body["session_token"] = session_token
            body["fence_token"] = fence_token
            body["session_token_ref"] = session_token_ref
            serialized = json.dumps(body, sort_keys=True)
            if _PLACEHOLDER.search(serialized):
                raise _graph_continuation_error(
                    "authenticated graph query body contains placeholders"
                )
            response, failure, raw_values = _invoke_host_tool(
                tool_caller,
                "graph_query",
                body,
                response_status=query["tool"],
                request_bodies=request_bodies,
                raw_results=raw_results,
                raw_values=raw_values,
            )
            if failure:
                return failure
            trace_id = _first_deep_text(
                response,
                "trace_id",
                "graph_trace_id",
                "graph_query_trace_id",
            )
            if not trace_id:
                raise _graph_continuation_error(
                    "graph query response omitted canonical trace id"
                )
            traces.append(trace_id)
            summaries.append(
                {
                    "tool": query["tool"],
                    "query": query["args"]["query"],
                    "trace_id": trace_id,
                }
            )
        return {
            "schema_version": RUNTIME_CONTEXT_GRAPH_CONTINUATION_SCHEMA_VERSION,
            "ok": True,
            "status": "passed",
            "session_token_ref": session_token_ref,
            "graph_trace_ids": traces,
            "queries": summaries,
            "guide_derived_body": True,
            "route_identity_preserved": True,
            **_HOST_PRIVACY_FLAGS,
        }
    finally:
        for value in (*request_bodies, *raw_results, application):
            if isinstance(value, (dict, list)):
                scrub_host_secret_values(value, raw_values=raw_values)
