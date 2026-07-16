"""Shared compatibility contract for long-lived MCP tool-schema clients.

The governance process and each MCP stdio process import this module at
startup.  After a governance hot upgrade, an already-running MCP process keeps
its loaded version while the new governance process advertises the current
version, making schema drift observable without coupling either process to the
other's in-memory tool table.
"""

from __future__ import annotations

from typing import Any


MCP_TOOL_SCHEMA_VERSION = "2026-07-16.1"
MCP_TOOL_SCHEMA_MIN_CLIENT_VERSION = MCP_TOOL_SCHEMA_VERSION


def mcp_loaded_tool_schema_metadata() -> dict[str, Any]:
    """Describe only the schema frozen into the current MCP process."""

    return {
        "schema_version": "mcp_loaded_tool_schema.v1",
        "loaded_client_tool_schema_version": MCP_TOOL_SCHEMA_VERSION,
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
) -> dict[str, Any]:
    """Build a stable, copy-safe schema freshness diagnostic."""

    loaded = str(loaded_schema_version or "").strip()
    server = str(server_schema_version or MCP_TOOL_SCHEMA_VERSION).strip()
    minimum = str(
        minimum_client_schema_version or MCP_TOOL_SCHEMA_MIN_CLIENT_VERSION
    ).strip()
    fresh = loaded == server if loaded else None
    return {
        "schema_version": "mcp_tool_schema_compatibility.v1",
        "loaded_client_tool_schema_version": loaded,
        "server_tool_schema_version": server,
        "minimum_client_tool_schema_version": minimum,
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
