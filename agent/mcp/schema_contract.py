"""Shared compatibility contract for long-lived MCP tool-schema clients.

The governance process and each MCP stdio process import this module at
startup.  After a governance hot upgrade, an already-running MCP process keeps
its loaded version while the new governance process advertises the current
version, making schema drift observable without coupling either process to the
other's in-memory tool table.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


MCP_TOOL_SCHEMA_VERSION = "2026-09-01.1"
MCP_TOOL_SCHEMA_MIN_CLIENT_VERSION = MCP_TOOL_SCHEMA_VERSION
_LOADED_TOOL_SCHEMA_FINGERPRINT = ""
_TOOL_SCHEMA_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def mcp_tool_schema_fingerprint(tools: list[dict[str, Any]]) -> str:
    """Return a deterministic digest of the exact loaded MCP tool registry."""

    canonical = json.dumps(
        tools,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def register_loaded_tool_schema(tools: list[dict[str, Any]]) -> str:
    """Bind metadata to the exact registry loaded by this MCP process."""

    global _LOADED_TOOL_SCHEMA_FINGERPRINT
    _LOADED_TOOL_SCHEMA_FINGERPRINT = mcp_tool_schema_fingerprint(tools)
    return _LOADED_TOOL_SCHEMA_FINGERPRINT


def resolve_server_tool_schema_fingerprint(
    *,
    nested_value: Any,
    nested_present: bool,
    top_level_value: Any,
    top_level_present: bool,
) -> dict[str, Any]:
    """Resolve two server fingerprint signals without source precedence."""

    nested_text = nested_value if isinstance(nested_value, str) else ""
    top_level_text = top_level_value if isinstance(top_level_value, str) else ""
    nested_valid = bool(
        nested_present and _TOOL_SCHEMA_FINGERPRINT_PATTERN.fullmatch(nested_text)
    )
    top_level_valid = bool(
        top_level_present
        and _TOOL_SCHEMA_FINGERPRINT_PATTERN.fullmatch(top_level_text)
    )
    both_present = nested_present and top_level_present
    conflict = bool(
        both_present
        and nested_valid
        and top_level_valid
        and nested_text != top_level_text
    )
    if not nested_present and not top_level_present:
        status = "absent"
        resolved = ""
    elif both_present:
        if nested_valid and top_level_valid and not conflict:
            status = "resolved_both_exact"
            resolved = nested_text
        else:
            status = "conflict" if conflict else "invalid"
            resolved = ""
    elif nested_present:
        status = "resolved_nested" if nested_valid else "invalid"
        resolved = nested_text if nested_valid else ""
    else:
        status = "resolved_top_level" if top_level_valid else "invalid"
        resolved = top_level_text if top_level_valid else ""
    return {
        "nested_present": nested_present,
        "nested_valid": nested_valid,
        "nested_value": nested_text,
        "nested_value_type": type(nested_value).__name__,
        "top_level_present": top_level_present,
        "top_level_valid": top_level_valid,
        "top_level_value": top_level_text,
        "top_level_value_type": type(top_level_value).__name__,
        "conflict": conflict,
        "status": status,
        "resolved_fingerprint": resolved,
    }


def mcp_loaded_tool_schema_metadata() -> dict[str, Any]:
    """Describe only the schema frozen into the current MCP process."""

    return {
        "schema_version": "mcp_loaded_tool_schema.v1",
        "loaded_client_tool_schema_version": MCP_TOOL_SCHEMA_VERSION,
        "loaded_client_tool_schema_fingerprint": _LOADED_TOOL_SCHEMA_FINGERPRINT,
        "server_tool_schema_version": "",
        "minimum_client_tool_schema_version": "",
        "client_schema_fresh": None,
        "server_version_observable": False,
        "stale_client_possible": True,
        "status": "process_local_loaded_schema",
        "freshness_signal": {
            "mcp_tool": "runtime_status",
            "response_path": "mcp_tool_schema",
            "tools_list_path": "_meta.aming_claw_tool_schema",
        },
        "refresh_action": "restart_or_refresh_mcp_session",
    }


def qa_session_register_http_fallback() -> dict[str, Any]:
    """Return the copy-safe HTTP fallback for a schema-lagging MCP session."""

    return {
        "method": "POST",
        "path": "/api/role/assign",
        "auth": "coordinator X-Gov-Token header",
        "body_fields": [
            "project_id",
            "principal_id",
            "role=qa",
            "backlog_id",
            "task_id",
            "commit_sha (full git object id)",
        ],
        "raw_token_handling": (
            "The HTTP response contains a one-time QA token; keep it out of "
            "timeline, backlog, code, docs, and model-visible evidence. Prefer "
            "refreshing the MCP session so managed qa_session_token_ref is used."
        ),
    }


def mcp_tool_schema_compatibility(
    *,
    loaded_schema_version: str = "",
    server_schema_version: str = MCP_TOOL_SCHEMA_VERSION,
    minimum_client_schema_version: str = MCP_TOOL_SCHEMA_MIN_CLIENT_VERSION,
    loaded_schema_fingerprint: str = "",
    server_schema_fingerprint: str = "",
) -> dict[str, Any]:
    """Build a stable, copy-safe schema freshness diagnostic."""

    loaded = str(loaded_schema_version or "").strip()
    server = str(server_schema_version or MCP_TOOL_SCHEMA_VERSION).strip()
    minimum = str(
        minimum_client_schema_version or MCP_TOOL_SCHEMA_MIN_CLIENT_VERSION
    ).strip()
    loaded_fingerprint = str(loaded_schema_fingerprint or "").strip()
    server_fingerprint = str(server_schema_fingerprint or "").strip()
    version_fresh = loaded == server if loaded else None
    fingerprint_fresh = bool(
        _TOOL_SCHEMA_FINGERPRINT_PATTERN.fullmatch(loaded_fingerprint)
        and _TOOL_SCHEMA_FINGERPRINT_PATTERN.fullmatch(server_fingerprint)
        and loaded_fingerprint == server_fingerprint
    )
    fresh = bool(version_fresh and fingerprint_fresh)
    return {
        "schema_version": "mcp_tool_schema_compatibility.v1",
        "loaded_client_tool_schema_version": loaded,
        "server_tool_schema_version": server,
        "minimum_client_tool_schema_version": minimum,
        "loaded_client_tool_schema_fingerprint": loaded_fingerprint,
        "server_tool_schema_fingerprint": server_fingerprint,
        "client_schema_fingerprint_fresh": fingerprint_fresh,
        "client_schema_fresh": fresh,
        "stale_client_possible": fresh is not True,
        "freshness_signal": {
            "mcp_tool": "runtime_status",
            "response_path": "mcp_tool_schema",
            "tools_list_path": "_meta.aming_claw_tool_schema",
        },
        "refresh_action": "restart_or_refresh_mcp_session",
        "http_fallback": qa_session_register_http_fallback(),
    }
