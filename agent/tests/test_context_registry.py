from __future__ import annotations

import sqlite3

import pytest

from agent.governance.db import _configure_connection, _ensure_schema
from agent.governance.context_registry import (
    FALLBACK_PACK_ID,
    ContextRegistryError,
    get_context_pack,
    resolve_context,
    seed_private_context_from_file,
    upsert_context_pack,
)
from agent.mcp.tools import TOOLS, ToolDispatcher


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _configure_connection(conn, busy_timeout=0)
    _ensure_schema(conn)
    return conn


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


def test_observer_resolves_private_pack_with_body_and_audit_metadata():
    conn = _conn()
    upsert_context_pack(
        conn,
        project_id="proj",
        pack_id="private_founder_paradigm.v1",
        title="Private founder context",
        visibility="private_founder",
        allowed_roles=["observer"],
        body="private observer judgment",
        summary="private summary",
    )

    result = resolve_context(conn, project_id="proj", role="observer", mode="design")

    assert result["ok"] is True
    private_pack = next(p for p in result["packs"] if p["pack_id"] == "private_founder_paradigm.v1")
    assert private_pack["body"] == "private observer judgment"
    assert private_pack["body_redacted"] is False
    assert result["selected_packs"][0]["body_redacted"] is True
    assert "private observer judgment" in result["context_text"]


def test_private_pack_is_rejected_when_allowed_for_worker_role():
    conn = _conn()

    with pytest.raises(ContextRegistryError):
        upsert_context_pack(
            conn,
            project_id="proj",
            pack_id="bad-private.v1",
            visibility="private_founder",
            allowed_roles=["observer", "mf_sub"],
            body="must not reach workers",
        )


def test_private_pack_is_blocked_for_mf_sub_resolution():
    conn = _conn()
    upsert_context_pack(
        conn,
        project_id="proj",
        pack_id="private_founder_paradigm.v1",
        visibility="private_founder",
        allowed_roles=["observer"],
        body="must not reach workers",
    )
    upsert_context_pack(
        conn,
        project_id="proj",
        pack_id="worker-safe.v1",
        visibility="task_context",
        allowed_roles=["mf_sub"],
        body="worker contract rule",
    )

    result = resolve_context(conn, project_id="proj", role="mf_sub", mode="implementation")

    assert [p["pack_id"] for p in result["packs"]] == ["worker-safe.v1"]
    assert result["packs"][0]["body"] == "worker contract rule"
    assert result["blocked_packs"][0]["pack_id"] == "private_founder_paradigm.v1"
    assert result["blocked_packs"][0]["reason"] == "private_founder_observer_only"
    assert "must not reach workers" not in result["context_text"]


def test_fallback_doc_resolves_for_observer_when_db_is_empty():
    conn = _conn()

    result = resolve_context(conn, project_id="proj", role="observer", mode="design")

    assert result["count"] == 1
    assert result["packs"][0]["pack_id"] == FALLBACK_PACK_ID
    assert result["packs"][0]["source_type"] == "fallback_doc"
    assert "Observer-Safe Expertise Routing" in result["context_text"]


def test_seed_private_file_imports_body_without_source_export(tmp_path):
    conn = _conn()
    source = tmp_path / "private.md"
    source.write_text("private local context", encoding="utf-8")

    pack = seed_private_context_from_file(
        conn,
        project_id="proj",
        source_path=str(source),
        created_by="test",
    )
    redacted = get_context_pack(
        conn,
        project_id="proj",
        pack_id=pack["pack_id"],
        role="mf_sub",
        include_body=True,
    )

    assert pack["visibility"] == "private_founder"
    assert pack["body"] == "private local context"
    assert pack["no_export"] is True
    assert redacted is not None
    assert redacted["body_redacted"] is True
    assert redacted["source_path_redacted"] is True
    assert "body" not in redacted


def test_mcp_context_pack_tools_resolve_in_process(monkeypatch):
    conn = _conn()
    from agent.governance import db

    monkeypatch.setattr(db, "get_connection", lambda *_args, **_kwargs: conn)
    assert {
        "context_pack_list",
        "context_pack_get",
        "context_pack_upsert",
        "context_pack_resolve",
        "context_pack_seed_private_file",
    }.issubset(_tool_names())

    dispatcher = ToolDispatcher(
        api_fn=lambda method, path, data=None: {"ok": True},
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=lambda method, path, data=None: {"ok": True},
        workspace=".",
    )
    upserted = dispatcher.dispatch(
        "context_pack_upsert",
        {
            "project_id": "proj",
            "pack_id": "observer-private.v1",
            "visibility": "private_founder",
            "allowed_roles": ["observer"],
            "body": "observer-only body",
        },
    )
    observer = dispatcher.dispatch(
        "context_pack_resolve",
        {"project_id": "proj", "role": "observer", "include_body": True},
    )
    worker = dispatcher.dispatch(
        "context_pack_resolve",
        {"project_id": "proj", "role": "mf_sub", "include_body": True},
    )

    assert upserted["ok"] is True
    assert "observer-only body" in observer["context_text"]
    assert "observer-only body" not in worker["context_text"]
    assert worker["blocked_packs"][0]["pack_id"] == "observer-private.v1"
