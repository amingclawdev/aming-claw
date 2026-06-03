from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.governance import observer_session, raw_requirement


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    observer_session.ensure_schema(conn)
    raw_requirement.ensure_schema(conn)
    return conn


def _register(conn: sqlite3.Connection, project_id: str = "demo") -> dict:
    return observer_session.register_session(conn, project_id=project_id)


def _execute_backlog_row_payload() -> dict:
    return {
        "backlog_id": "AC-ROUTE-HANDOFF",
        "route_id": "route-20260602-9cbbd7a9fd",
        "route_context_hash": "sha256:f1641a8d28b2a9211a14d90fed8dda4c40bb87380557f64a81e29e332568c27b",
        "prompt_contract_id": "rprompt-7417905f707deac2",
        "visible_injection_manifest_hash": "sha256:30e229df0e1948f6c206d954c8226acd9272816a4168216a4258a8ebf0328810",
        "subsystem": "observer",
    }


def test_command_enqueue_and_list_preserve_business_payload_in_db():
    conn = _conn()

    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1", "source": "dashboard"},
        created_by="dashboard",
    )
    listed = observer_session.list_commands(conn, project_id="demo")

    assert command["status"] == observer_session.COMMAND_STATUS_QUEUED
    assert command["payload"] == {"raw_id": "raw-1", "source": "dashboard"}
    assert listed[0]["command_id"] == command["command_id"]
    assert listed[0]["payload"]["raw_id"] == "raw-1"
    assert observer_session.command_pending_reminder("demo") == {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
        "next_action": {
            "tool": "observer_command_next",
            "description": "claim the next pending observer command",
        },
    }


def test_execute_backlog_row_claim_complete_preserves_payload_without_reminder_leak():
    conn = _conn()
    session = _register(conn)
    payload = _execute_backlog_row_payload()

    assert observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW in observer_session.VALID_COMMAND_TYPES
    assert (
        observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW
        in observer_session.DEFAULT_CAPABILITIES["command_types"]
    )

    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=payload,
        created_by="judgment_brain",
        notify=True,
    )
    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={"ok": True, "backlog_id": payload["backlog_id"]},
    )

    reminder = observer_session.command_pending_reminder("demo")

    assert command["payload"] == payload
    assert claimed["command"]["payload"] == payload
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert completed["command"]["payload"] == payload
    assert completed["command"]["result"] == {"ok": True, "backlog_id": payload["backlog_id"]}
    assert reminder["payload_included"] is False
    assert "payload" not in reminder
    assert payload["backlog_id"] not in str(reminder)


def test_execute_backlog_row_rejects_missing_route_payload_fields():
    conn = _conn()

    with pytest.raises(ValueError, match="payload must be an object.*backlog_id"):
        observer_session.enqueue_command(
            conn,
            project_id="demo",
            command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            payload=None,
        )

    with pytest.raises(
        ValueError,
        match=(
            "missing required fields: route_id, route_context_hash, "
            "prompt_contract_id, visible_injection_manifest_hash"
        ),
    ):
        observer_session.enqueue_command(
            conn,
            project_id="demo",
            command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
            payload={"backlog_id": "AC-ROUTE-HANDOFF"},
        )


def test_mcp_observer_command_enqueue_schema_accepts_execute_backlog_row():
    from agent.mcp.tools import TOOLS

    enqueue_tool = next(tool for tool in TOOLS if tool.get("name") == "observer_command_enqueue")
    command_type = enqueue_tool["inputSchema"]["properties"]["command_type"]

    assert observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW in command_type["enum"]


def test_claim_requires_valid_token_and_project_match():
    conn = _conn()
    session = _register(conn)
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CONFIRM_REQUIREMENT,
        payload={"raw_id": "raw-1"},
    )

    with pytest.raises(observer_session.ObserverAuthError):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=session["session_id"],
            session_token="wrong",
        )

    with pytest.raises(observer_session.ObserverPermissionError):
        observer_session.claim_command(
            conn,
            project_id="other",
            session_id=session["session_id"],
            session_token=session["session_token"],
        )


def test_claim_is_idempotent_for_same_session_and_rejects_double_claim():
    conn = _conn()
    session = _register(conn)
    other = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_PAUSE_WORKER,
        payload={"task_id": "task-1"},
    )

    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    repeated = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )

    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert repeated["command"]["command_id"] == command["command_id"]

    with pytest.raises(observer_session.ObserverCommandConflict):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=other["session_id"],
            session_token=other["session_token"],
            command_id=command["command_id"],
        )


def test_stale_claimed_command_can_be_taken_over_by_fallback_session():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:03:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
        payload=_execute_backlog_row_payload(),
        created_by="judgment_brain",
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    takeover = observer_session.takeover_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        reason="fallback observer resolves stale claimed command",
        now="2026-06-03T00:03:01Z",
    )

    assert takeover["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert takeover["takeover"]["previous_session_id"] == owner["session_id"]
    assert takeover["takeover"]["previous_session_status"] == "stale"

    observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        now="2026-06-03T00:03:02Z",
    )
    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.complete_command(
            conn,
            project_id="demo",
            session_id=owner["session_id"],
            session_token=owner["session_token"],
            command_id=command["command_id"],
            result={"ok": True},
            now="2026-06-03T00:03:03Z",
        )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        result={"ok": True, "takeover": takeover["takeover"]},
        now="2026-06-03T00:03:04Z",
    )
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert completed["command"]["result"]["takeover"]["previous_session_status"] == "stale"


def test_active_claimed_command_cannot_be_taken_over():
    conn = _conn()
    owner = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-owner",
        now="2026-06-03T00:00:00Z",
    )
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:00:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CANCEL_WORKER,
        payload={"task_id": "task-1"},
        now="2026-06-03T00:00:01Z",
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=owner["session_id"],
        session_token=owner["session_token"],
        command_id=command["command_id"],
        now="2026-06-03T00:00:02Z",
    )

    with pytest.raises(observer_session.ObserverCommandConflict, match="not stale: active"):
        observer_session.takeover_command(
            conn,
            project_id="demo",
            session_id=fallback["session_id"],
            session_token=fallback["session_token"],
            command_id=command["command_id"],
            reason="fallback observer tries to steal active command",
            now="2026-06-03T00:00:03Z",
        )


def test_missing_owner_claimed_command_can_be_taken_over():
    conn = _conn()
    fallback = observer_session.register_session(
        conn,
        project_id="demo",
        session_id="obs-fallback",
        now="2026-06-03T00:00:00Z",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_PAUSE_WORKER,
        payload={"task_id": "task-1"},
        now="2026-06-03T00:00:01Z",
    )
    conn.execute(
        """UPDATE observer_command_queue
              SET status = ?, claimed_by_session_id = ?, claimed_at = ?
            WHERE command_id = ?""",
        (
            observer_session.COMMAND_STATUS_CLAIMED,
            "obs-missing",
            "2026-06-03T00:00:02Z",
            command["command_id"],
        ),
    )
    conn.commit()

    takeover = observer_session.takeover_command(
        conn,
        project_id="demo",
        session_id=fallback["session_id"],
        session_token=fallback["session_token"],
        command_id=command["command_id"],
        reason="fallback observer resolves missing owner",
        now="2026-06-03T00:00:03Z",
    )

    assert takeover["takeover"]["previous_session_id"] == "obs-missing"
    assert takeover["takeover"]["previous_session_status"] == "missing"
    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]


def test_complete_and_fail_require_same_claimed_session():
    conn = _conn()
    session = _register(conn)
    other = _register(conn)
    complete_command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CONTINUE_WORKER,
        payload={"task_id": "task-1"},
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=complete_command["command_id"],
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.complete_command(
            conn,
            project_id="demo",
            session_id=other["session_id"],
            session_token=other["session_token"],
            command_id=complete_command["command_id"],
            result={"ok": True},
        )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=complete_command["command_id"],
        result={"ok": True},
    )
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED

    fail_command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CANCEL_WORKER,
        payload={"task_id": "task-2"},
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=other["session_id"],
        session_token=other["session_token"],
        command_id=fail_command["command_id"],
    )
    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.fail_command(
            conn,
            project_id="demo",
            session_id=session["session_id"],
            session_token=session["session_token"],
            command_id=fail_command["command_id"],
            error="wrong owner",
        )

    failed = observer_session.fail_command(
        conn,
        project_id="demo",
        session_id=other["session_id"],
        session_token=other["session_token"],
        command_id=fail_command["command_id"],
        error="cancel rejected",
    )
    assert failed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert failed["command"]["error"] == "cancel rejected"


def test_actor_self_report_does_not_authorize_command_claim():
    from agent.governance import server

    conn = _conn()
    ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={"actor": "observer"},
        get_project_id=lambda: "demo",
    )

    with patch("agent.governance.server.get_connection", return_value=conn):
        code, payload = server.handle_observer_command_claim(ctx)

    assert code == 401
    assert payload["error"] == "observer_auth_failed"


def test_api_enqueue_publishes_reminder_only_event_and_preserves_command_payload():
    from agent.governance import event_bus, server

    conn = _conn()
    captured: list[dict] = []

    def on_pending(payload: dict) -> None:
        captured.append(payload)

    ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={
            "command_type": observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
            "payload": {"raw_id": "raw-1", "source": "dashboard"},
            "created_by": "dashboard",
        },
        get_project_id=lambda: "demo",
    )

    bus = event_bus.get_event_bus()
    bus.subscribe("observer_command_pending", on_pending)
    try:
        with patch("agent.governance.server.get_connection", return_value=conn):
            code, payload = server.handle_observer_command_enqueue(ctx)
    finally:
        bus.unsubscribe("observer_command_pending", on_pending)

    expected_reminder = {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
        "next_action": {
            "tool": "observer_command_next",
            "description": "claim the next pending observer command",
        },
    }
    command = payload["observer_command"]

    assert code == 201
    assert payload["hook_reminder"] == expected_reminder
    assert captured == [expected_reminder]
    assert set(captured[0]) == {
        "kind",
        "project_id",
        "message",
        "payload_included",
        "next_action",
    }
    assert "raw_id" not in captured[0]
    assert "command_type" not in captured[0]
    assert "source" not in captured[0]
    assert "command_id" not in captured[0]
    assert command["payload"] == {"raw_id": "raw-1", "source": "dashboard"}
    assert command["status"] == observer_session.COMMAND_STATUS_NOTIFIED
    assert command["notified_at"]

    session = _register(conn)
    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED


def test_analyze_complete_projects_raw_requirement_to_confirmation():
    conn = _conn()
    session = _register(conn)
    raw = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="Let users drag captured requirements into execution",
        source="dashboard",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": raw["raw_id"]},
        created_by="dashboard",
    )

    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            "raw_id": raw["raw_id"],
            "ai_interpretation": "User wants a queue promotion control.",
            "proposed_backlog_mapping": {
                "bug_id": "REQ-QUEUE-PROMOTE",
                "title": "Promote raw requirements to execution queue",
            },
        },
    )

    updated = raw_requirement.get_raw_requirement(conn, project_id="demo", raw_id=raw["raw_id"])
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert updated["status"] == raw_requirement.STATUS_NEEDS_CONFIRMATION
    assert "AI interpretation" in updated["note"]
    assert "REQ-QUEUE-PROMOTE" in updated["note"]


def test_api_takeover_rejects_active_owner_then_allows_closed_owner_terminal_fail():
    from agent.governance import server

    conn = _conn()

    def ctx(path_params: dict, body: dict | None = None):
        return SimpleNamespace(
            path_params=path_params,
            query={},
            body=body or {},
            get_project_id=lambda: "demo",
        )

    with (
        patch("agent.governance.server.get_connection", return_value=conn),
        patch("agent.governance.server.audit_service.record", return_value={"ok": True}),
    ):
        owner_code, owner = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {
                    "observer_kind": "codex",
                    "session_id": "obs-owner",
                    "session_label": "owner",
                },
            )
        )
        fallback_code, fallback = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {
                    "observer_kind": "codex",
                    "session_id": "obs-fallback",
                    "session_label": "fallback",
                },
            )
        )
        enqueue_code, enqueue = server.handle_observer_command_enqueue(
            ctx(
                {"project_id": "demo"},
                {
                    "command_type": observer_session.COMMAND_TYPE_EXECUTE_BACKLOG_ROW,
                    "payload": _execute_backlog_row_payload(),
                    "created_by": "judgment_brain",
                },
            )
        )
        command_id = enqueue["observer_command"]["command_id"]

        claimed = server.handle_observer_command_claim(
            ctx(
                {"project_id": "demo"},
                {
                    "session_id": owner["session_id"],
                    "session_token": owner["session_token"],
                    "command_id": command_id,
                },
            )
        )
        active_takeover_code, active_takeover = server.handle_observer_command_takeover(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": fallback["session_id"],
                    "session_token": fallback["session_token"],
                    "reason": "fallback observer tries to steal active command",
                },
            )
        )

        closed = server.handle_observer_session_close(
            ctx(
                {"project_id": "demo", "session_id": owner["session_id"]},
                {
                    "session_token": owner["session_token"],
                },
            )
        )
        takeover = server.handle_observer_command_takeover(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": fallback["session_id"],
                    "session_token": fallback["session_token"],
                    "reason": "fallback observer resolves closed owner",
                },
            )
        )
        failed = server.handle_observer_command_fail(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": fallback["session_id"],
                    "session_token": fallback["session_token"],
                    "error": "fallback completed stale owner takeover",
                    "result": {"takeover": takeover["takeover"]},
                },
            )
        )

    assert owner_code == 201
    assert fallback_code == 201
    assert enqueue_code == 201
    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert claimed["command"]["claimed_by_session_id"] == owner["session_id"]

    assert active_takeover_code == 409
    assert active_takeover["error"] == "observer_command_conflict"
    assert "not stale: active" in active_takeover["message"]

    assert closed["status"] == observer_session.SESSION_STATUS_CLOSED
    assert takeover["ok"] is True
    assert takeover["observer_session_id"] == fallback["session_id"]
    assert takeover["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert takeover["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert takeover["takeover"]["previous_session_id"] == owner["session_id"]
    assert takeover["takeover"]["previous_session_status"] == observer_session.SESSION_STATUS_CLOSED

    assert failed["observer_session_id"] == fallback["session_id"]
    assert failed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert failed["command"]["claimed_by_session_id"] == fallback["session_id"]
    assert failed["command"]["result"]["takeover"]["previous_session_status"] == (
        observer_session.SESSION_STATUS_CLOSED
    )


def test_api_smoke_capture_enqueue_claim_complete_reflects_project_inbox():
    from agent.governance import server

    conn = _conn()

    def ctx(path_params: dict, body: dict | None = None):
        return SimpleNamespace(
            path_params=path_params,
            query={},
            body=body or {},
            get_project_id=lambda: "demo",
        )

    with patch("agent.governance.server.get_connection", return_value=conn):
        register_code, register_payload = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {"observer_kind": "codex", "session_label": "smoke"},
            )
        )
        create_code, create_payload = server.handle_project_raw_requirement_create(
            ctx(
                {"project_id": "demo"},
                {
                    "raw_text": "Add one button that asks the observer to analyze this",
                    "source": "dashboard_project_inbox",
                    "actor": "dashboard",
                },
            )
        )
        raw_id = create_payload["raw_requirement"]["raw_id"]
        enqueue_code, enqueue_payload = server.handle_observer_command_enqueue(
            ctx(
                {"project_id": "demo"},
                {
                    "command_type": "analyze_requirements",
                    "payload": {"raw_id": raw_id, "source": "project_inbox"},
                    "created_by": "dashboard",
                },
            )
        )
        command_id = enqueue_payload["observer_command"]["command_id"]
        claim_payload = server.handle_observer_command_claim(
            ctx(
                {"project_id": "demo"},
                {
                    "session_id": register_payload["session_id"],
                    "session_token": register_payload["session_token"],
                    "command_id": command_id,
                },
            )
        )
        complete_payload = server.handle_observer_command_complete(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": register_payload["session_id"],
                    "session_token": register_payload["session_token"],
                    "result": {
                        "raw_id": raw_id,
                        "ai_interpretation": "User wants dashboard command queue wiring.",
                        "proposed_backlog_mapping": {
                            "bug_id": "SMOKE-OBSERVER-COMMAND",
                            "title": "Wire AI Analyze to observer command queue",
                        },
                    },
                },
            )
        )
        inbox = server.handle_project_inbox(ctx({"project_id": "demo"}))

    assert register_code == 201
    assert create_code == 201
    assert enqueue_code == 201
    assert claim_payload["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert complete_payload["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert inbox["lanes"]["raw_inbox"]["count"] == 0
    assert inbox["lanes"]["needs_confirmation"]["count"] == 1
    assert inbox["observer"]["connected"] is True
    assert inbox["observer_commands"]["counts"]["completed"] == 1
