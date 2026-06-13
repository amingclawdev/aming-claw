from __future__ import annotations

import sqlite3

import pytest

from agent.governance import observer_session


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    observer_session.ensure_schema(conn)
    return conn


def test_register_returns_token_once_and_stores_only_hash():
    conn = _conn()

    result = observer_session.register_session(
        conn,
        project_id="demo",
        observer_kind="codex",
        session_label="local observer",
        pid=123,
        cwd="/tmp/demo",
        now="2026-05-28T00:00:00Z",
    )

    session_id = result["observer_session_id"]
    token = result["session_token"]
    assert session_id
    assert token
    assert result["heartbeat_interval_sec"] == observer_session.HEARTBEAT_INTERVAL_SEC

    stored = conn.execute(
        "SELECT token_hash FROM observer_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert stored["token_hash"].startswith("sha256:")
    assert stored["token_hash"] != token
    assert token not in stored["token_hash"]

    fetched = observer_session.get_session(conn, project_id="demo", session_id=session_id)
    listed = observer_session.list_sessions(conn, project_id="demo")
    assert "session_token" not in fetched
    assert "token_hash" not in fetched
    assert "session_token" not in listed[0]
    assert "token_hash" not in listed[0]


def test_heartbeat_updates_last_seen_and_restores_active_status():
    conn = _conn()
    result = observer_session.register_session(
        conn,
        project_id="demo",
        now="2026-05-28T00:00:00Z",
    )

    heartbeat = observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        session_token=result["session_token"],
        now="2026-05-28T00:01:00Z",
    )

    assert heartbeat["session"]["last_seen_at"] == "2026-05-28T00:01:00Z"
    assert heartbeat["session"]["computed_status"] == "active"


def test_stale_status_is_computed_from_last_seen():
    conn = _conn()
    result = observer_session.register_session(
        conn,
        project_id="demo",
        now="2026-05-28T00:00:00Z",
    )

    current = observer_session.get_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        now="2026-05-28T00:00:30Z",
    )
    idle = observer_session.get_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        now="2026-05-28T00:01:30Z",
    )
    stale = observer_session.get_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        now="2026-05-28T00:03:00Z",
    )

    assert current["computed_status"] == "active"
    assert idle["computed_status"] == "idle"
    assert stale["computed_status"] == "stale"


def test_auth_rejects_wrong_token_and_wrong_project():
    conn = _conn()
    result = observer_session.register_session(conn, project_id="demo")

    with pytest.raises(observer_session.ObserverAuthError):
        observer_session.heartbeat_session(
            conn,
            project_id="demo",
            session_id=result["session_id"],
            session_token="wrong",
        )

    with pytest.raises(observer_session.ObserverPermissionError):
        observer_session.heartbeat_session(
            conn,
            project_id="other",
            session_id=result["session_id"],
            session_token=result["session_token"],
        )


def test_revoked_session_rejects_privileged_command_claim():
    conn = _conn()
    result = observer_session.register_session(conn, project_id="demo")
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1"},
    )
    observer_session.revoke_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        session_token=result["session_token"],
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="revoked"):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=result["session_id"],
            session_token=result["session_token"],
        )


def test_stale_session_rejects_privileged_command_claim():
    conn = _conn()
    result = observer_session.register_session(
        conn,
        project_id="demo",
        now="2026-05-28T00:00:00Z",
    )
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1"},
        now="2026-05-28T00:00:01Z",
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="stale"):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=result["session_id"],
            session_token=result["session_token"],
            now="2026-05-28T00:03:00Z",
        )


# ---------------------------------------------------------------------------
# Observer work-mode state + gate
# ---------------------------------------------------------------------------
def test_work_mode_default_is_observer_look_before_act():
    assert observer_session.DEFAULT_WORK_MODE == "observer_look_before_act"
    assert observer_session.normalize_work_mode(None) == "observer_look_before_act"
    assert observer_session.normalize_work_mode("garbage") == "observer_look_before_act"
    assert (
        observer_session.normalize_work_mode("observer-execution-supervisor")
        == "observer_execution_supervisor"
    )


def test_look_before_act_blocks_implementation_dispatch_merge_close():
    for action in (
        "edit_implementation",
        "self_clear_judge_blocker",
        "dispatch_implementation",
        "merge",
        "close",
    ):
        gate = observer_session.work_mode_action_gate(
            "observer_look_before_act", action
        )
        assert gate["allowed"] is False, action
        assert gate["blocked"] is True
    # read/inspect/file-findings/propose-next stay allowed.
    for action in ("read", "inspect", "file_findings", "propose_next"):
        gate = observer_session.work_mode_action_gate(
            "observer_look_before_act", action
        )
        assert gate["allowed"] is True, action


def test_execution_supervisor_unlocks_coordination_but_not_implementation():
    allow = observer_session.work_mode_action_gate(
        "observer_execution_supervisor", "dispatch_implementation"
    )
    assert allow["allowed"] is True
    for action in ("merge", "close"):
        assert observer_session.work_mode_action_gate(
            "observer_execution_supervisor", action
        )["allowed"] is True
    # Direct implementation / judge self-clear are never allowed, even here.
    for action in ("edit_implementation", "self_clear_judge_blocker"):
        gate = observer_session.work_mode_action_gate(
            "observer_execution_supervisor", action
        )
        assert gate["allowed"] is False, action
        assert gate["reason"] == "observer_must_never_perform_this_action"


@pytest.mark.parametrize(
    "transition_marker",
    [
        {"event_kind": "observer_work_mode_transition"},
        {"event_type": "observer.work_mode_transition"},
    ],
)
def test_work_mode_transition_requires_event_and_bound_precheck(transition_marker):
    identity = {
        "route_id": "route-1",
        "route_context_hash": "sha256:ctx",
        "prompt_contract_id": "rprompt-1",
    }
    # Empty evidence: transition blocked, both pieces missing.
    blocked = observer_session.work_mode_transition_gate(
        [], canonical_route_identity=identity
    )
    assert blocked["allowed"] is False
    assert "work_mode_transition_event" in blocked["missing"]
    assert "route_action_precheck_bound_to_canonical_route" in blocked["missing"]

    # Transition event alone is still not enough.
    transition_event = {
        **transition_marker,
        "status": "accepted",
        "payload": {
            "from_work_mode": "observer_look_before_act",
            "to_work_mode": "observer_execution_supervisor",
        },
    }
    only_event = observer_session.work_mode_transition_gate(
        [transition_event], canonical_route_identity=identity
    )
    assert only_event["allowed"] is False
    assert only_event["missing"] == ["route_action_precheck_bound_to_canonical_route"]

    # A precheck bound to the WRONG identity does not unlock the transition.
    wrong_precheck = {
        "event_kind": "route_action_precheck",
        "status": "allowed",
        "payload": {"route_id": "route-OTHER", "route_context_hash": "sha256:ctx"},
    }
    assert observer_session.work_mode_transition_gate(
        [transition_event, wrong_precheck], canonical_route_identity=identity
    )["allowed"] is False

    # Transition event + precheck bound to canonical identity unlocks it.
    bound_precheck = {
        "event_kind": "route_action_precheck",
        "status": "allowed",
        "payload": dict(identity),
    }
    allowed = observer_session.work_mode_transition_gate(
        [transition_event, bound_precheck], canonical_route_identity=identity
    )
    assert allowed["allowed"] is True
    assert allowed["missing"] == []


# ---------------------------------------------------------------------------
# Observer root route context bootstrap
# ---------------------------------------------------------------------------
def test_root_route_context_returns_required_fields_with_default_mode():
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-OBSERVER-ROOT-ROUTE-CONTEXT-WORK-MODE-20260609",
        route_context={
            "route_id": "route-7",
            "prompt_contract_id": "rprompt-7",
            "route_context_hash": "sha256:ctx7",
        },
        loaded_skills=["aming-claw"],
        loaded_resources=["mf-sop.md"],
        graph_query_schema_trace_id="graph-trace-7",
    )
    for field in (
        "backlog_id",
        "route_id",
        "prompt_contract_id",
        "work_mode",
        "loaded_skills",
        "loaded_resources",
        "graph_query_schema_trace_id",
        "allowed_actions",
        "blocked_actions",
        "required_evidence",
        "next_legal_action",
    ):
        assert field in ctx, field
    assert ctx["work_mode"] == "observer_look_before_act"
    assert ctx["route_id"] == "route-7"
    assert ctx["prompt_contract_id"] == "rprompt-7"
    assert ctx["graph_query_schema_trace_id"] == "graph-trace-7"
    assert ctx["loaded_skills"] == ["aming-claw"]
    # In look-before-act the supervisor actions are blocked and the next legal
    # action is the work-mode transition.
    for blocked in ("edit_implementation", "dispatch_implementation", "merge", "close"):
        assert blocked in ctx["blocked_actions"], blocked
    assert ctx["next_legal_action"]["id"] == "record_work_mode_transition"
    assert "route_context" in ctx["required_evidence"]


def test_root_route_context_execution_supervisor_unblocks_dispatch():
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-X",
        work_mode="observer_execution_supervisor",
        route_context={"route_id": "route-9", "prompt_contract_id": "rprompt-9"},
    )
    assert ctx["work_mode"] == "observer_execution_supervisor"
    assert "dispatch_implementation" not in ctx["blocked_actions"]
    assert "dispatch_implementation" in ctx["allowed_actions"]
    # Direct implementation stays blocked regardless of mode.
    assert "edit_implementation" in ctx["blocked_actions"]
    assert ctx["next_legal_action"]["id"] == "dispatch_bounded_worker"


def test_root_route_context_surfaces_full_five_field_canonical_identity():
    """Regression for AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-INJECTION-MANIFEST-HASH.

    When the route_context the server assembled carries the manifest hash pinned by
    a route_identity_cleanup, the returned canonical identity MUST be a complete
    external identity: all five fields present and non-empty (route_id,
    route_context_hash, prompt_contract_id, prompt_contract_hash,
    visible_injection_manifest_hash). A fresh observer consuming this identity then
    passes external-identity validation instead of forking the route.
    """
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-INJECTION-MANIFEST-HASH-20260609",
        route_context={
            "route_id": "route-repair-8884b4374cb18e09",
            "route_context_hash": "sha256:ctx-a226bba",
            "prompt_contract_id": "rprompt-repair-8884b4374cb18e09",
            "prompt_contract_hash": "sha256:pc-a226bba",
            "visible_injection_manifest_hash": "sha256:vim-a226bba",
        },
    )

    identity = ctx["canonical_route_identity"]
    for field in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        assert field in identity, field
        assert identity[field], field
    # The manifest hash is now surfaced top-level and inside the identity verbatim.
    assert ctx["visible_injection_manifest_hash"] == "sha256:vim-a226bba"
    assert identity["visible_injection_manifest_hash"] == "sha256:vim-a226bba"
    assert identity["prompt_contract_hash"] == "sha256:pc-a226bba"
    # route_id is part of the five-field external identity too (the field the
    # server backfills from the pinning event) — assert it verbatim and non-empty.
    assert ctx["route_id"] == "route-repair-8884b4374cb18e09"
    assert identity["route_id"] == "route-repair-8884b4374cb18e09"
    # A complete identity is not flagged incomplete.
    assert ctx["canonical_route_identity_complete"] is True
    assert "incomplete" not in identity
    assert "missing_fields" not in identity


def test_root_route_context_missing_route_id_is_marked_not_fabricated():
    """Regression for AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-ROUTE-ID.

    route_id is part of the external identity but is NOT in the gate's
    route_identity summary, so when the route_context the server assembled does
    not carry it (and no pinning event supplied it), build_observer_root_route_context
    must keep the key PRESENT-but-empty and flag the identity incomplete with
    route_id in missing_fields — never fabricated. The companion endpoint test
    (test_root_route_context_backfills_route_id_from_pinning_event) covers the
    happy path where the server sources route_id from the pinning event.
    """
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-OBSERVER-ROOT-ROUTE-CONTEXT-MISSING-ROUTE-ID-20260609",
        route_context={
            # route_id intentionally absent (the residual-bug input).
            "route_context_hash": "sha256:ctx-a226bba",
            "prompt_contract_id": "rprompt-repair-8884b4374cb18e09",
            "prompt_contract_hash": "sha256:pc-a226bba",
            "visible_injection_manifest_hash": "sha256:vim-a226bba",
        },
    )

    identity = ctx["canonical_route_identity"]
    # The key is PRESENT (not dropped) but empty, and route_id is never fabricated.
    assert "route_id" in identity
    assert identity["route_id"] == ""
    assert ctx["route_id"] == ""
    # The incompleteness is explicit so a consumer can refuse to fork knowingly.
    assert ctx["canonical_route_identity_complete"] is False
    assert identity["incomplete"] is True
    assert identity["missing_fields"] == ["route_id"]
    assert identity["incomplete_reason"]


def test_root_route_context_missing_manifest_hash_is_marked_not_dropped():
    """Before/after fork-repro: the BUG was that the manifest hash key was dropped.

    BEFORE the fix the canonical identity omitted visible_injection_manifest_hash
    entirely, so external-identity validation could not even see it was missing and
    the consuming observer forked. AFTER the fix the key is always present (empty
    when genuinely unavailable) and the identity is explicitly flagged incomplete
    with the missing field listed — deterministic and inspectable, never fabricated.
    """
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-OBSERVER-ROOT-ROUTE-CONTEXT-FORK-REPRO-20260609",
        route_context={
            "route_id": "route-repair-8884b4374cb18e09",
            "route_context_hash": "sha256:ctx-a226bba",
            "prompt_contract_id": "rprompt-repair-8884b4374cb18e09",
            "prompt_contract_hash": "sha256:pc-a226bba",
            # visible_injection_manifest_hash intentionally absent (the bug input).
        },
    )

    identity = ctx["canonical_route_identity"]
    # The key is PRESENT (not dropped) but empty, and the hash is never fabricated.
    assert "visible_injection_manifest_hash" in identity
    assert identity["visible_injection_manifest_hash"] == ""
    assert ctx["visible_injection_manifest_hash"] == ""
    # The incompleteness is explicit so a consumer can refuse to fork knowingly.
    assert ctx["canonical_route_identity_complete"] is False
    assert identity["incomplete"] is True
    assert identity["missing_fields"] == ["visible_injection_manifest_hash"]
    assert identity["incomplete_reason"]


def test_command_blocks_close_for_claimed_or_nonterminal_status():
    # Criterion 3: a still-claimed (or owned/queued/notified) originating
    # command blocks the backlog close.
    assert observer_session.command_blocks_close("claimed") is True
    assert observer_session.command_blocks_close("running") is True
    assert observer_session.command_blocks_close("queued") is True
    assert observer_session.command_blocks_close("notified") is True
    # Empty / unknown disposition fails safe (blocks).
    assert observer_session.command_blocks_close("") is True
    assert observer_session.command_blocks_close(None) is True
    assert observer_session.command_blocks_close("mystery_state") is True


def test_command_blocks_close_clears_on_terminal_or_co_resolved():
    # Criterion 3: completed / failed / cancelled / co-resolved dispositions
    # do not block the close.
    assert observer_session.command_blocks_close("completed") is False
    assert observer_session.command_blocks_close("failed") is False
    assert observer_session.command_blocks_close("cancelled") is False
    assert observer_session.command_blocks_close("co_resolved") is False
    assert observer_session.command_blocks_close("co-resolved-with-close") is False
    assert observer_session.command_blocks_close("resolved") is False


# ---------------------------------------------------------------------------
# GAP 2 regression tests — bootstrap compact handoff (AC-OBSERVER-ROOT-ROUTE-
# BOOTSTRAP-COMPACT-HANDOFF-20260609)
# ---------------------------------------------------------------------------

_DOCUMENTED_COMPACT_FIELDS = {
    "backlog_id",
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "visible_injection_manifest_hash",
    "work_mode",
    "default_work_mode",
    "allowed_actions",
    "blocked_actions",
    "required_evidence",
    "loaded_skills",
    "loaded_resources",
    "graph_query_schema_trace_id",
    "next_legal_action",
    "canonical_route_identity",
}

# Provider names that must never appear in a public-safe response.
_PRIVATE_PROVIDER_NAMES = {"openai", "anthropic", "claude", "codex", "gpt"}


def _build_full_route_context() -> dict:
    return {
        "route_id": "route-20260610-abc123",
        "route_context_hash": "sha256:abc123ctx",
        "prompt_contract_id": "rprompt-abc123",
        "prompt_contract_hash": "sha256:abc123pc",
        "visible_injection_manifest_hash": "sha256:abc123vim",
    }


def test_compact_output_contains_exactly_documented_fields_no_raw_provider_body():
    """GAP 2 (i): compact default output contains exactly the documented fields
    and no raw provider body or provider-private fields.
    """
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-BOOTSTRAP-COMPACT-20260609",
        route_context=_build_full_route_context(),
        loaded_skills=["aming-claw"],
        loaded_resources=["mf-sop.md"],
        graph_query_schema_trace_id="gqt-20260610-abc123",
    )

    # Every documented field must be present.
    for field in _DOCUMENTED_COMPACT_FIELDS:
        assert field in ctx, f"missing documented field: {field}"

    # No raw provider body or provider-naming fields may leak into the response.
    ctx_str = str(ctx).lower()
    for provider in _PRIVATE_PROVIDER_NAMES:
        assert provider not in ctx_str, (
            f"provider name {provider!r} leaked into compact output"
        )


def test_public_safe_boundary_no_private_provider_names_or_raw_prompt_text():
    """GAP 2 (ii): public-safe boundary — response never contains private
    provider names or raw prompt text fields.
    """
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-BOOTSTRAP-PUBLIC-SAFE-20260609",
        route_context=_build_full_route_context(),
        loaded_skills=["aming-claw"],
        loaded_resources=["aming-claw://skill"],
    )

    response_str = str(ctx).lower()

    # No raw prompt text fields.
    assert "raw_prompt" not in response_str
    assert "raw_context" not in response_str
    assert "provider_body" not in response_str

    # No private provider names.
    for provider in _PRIVATE_PROVIDER_NAMES:
        assert provider not in response_str, (
            f"private provider name {provider!r} must not appear in public response"
        )


def test_no_recorded_route_identity_returns_valid_look_before_act_handoff():
    """GAP 2 (iii): when the row has no recorded route identity, the response
    still returns a valid look-before-act handoff with a defined next_legal_action.
    Asserts the REAL current behavior, not invented behavior.
    """
    # Supply no route_context — simulates a backlog row that has no recorded
    # route identity (no route_context gate has fired yet).
    ctx = observer_session.build_observer_root_route_context(
        backlog_id="AC-BOOTSTRAP-NO-ROUTE-IDENTITY-20260609",
    )

    # Must be a valid handoff regardless — no exception, all mandatory fields
    # present, and next_legal_action resolves to the look-before-act blocker.
    for field in _DOCUMENTED_COMPACT_FIELDS:
        assert field in ctx, f"missing field with no route identity: {field}"

    # With no recorded identity the canonical identity is incomplete but present.
    identity = ctx["canonical_route_identity"]
    assert "route_id" in identity
    assert "route_context_hash" in identity
    assert "prompt_contract_id" in identity
    assert "prompt_contract_hash" in identity
    assert "visible_injection_manifest_hash" in identity

    # All five fields are empty (not fabricated) when no identity is available.
    for field in (
        "route_id",
        "route_context_hash",
        "prompt_contract_id",
        "prompt_contract_hash",
        "visible_injection_manifest_hash",
    ):
        assert identity[field] == "", (
            f"field {field!r} should be empty-not-fabricated when no route identity"
        )

    # Identity is flagged incomplete.
    assert ctx["canonical_route_identity_complete"] is False
    assert identity.get("incomplete") is True

    # The next legal action is the real look-before-act blocker.
    nla = ctx["next_legal_action"]
    assert nla["id"] == "record_work_mode_transition"


def test_next_legal_action_differs_between_modes():
    """GAP 2 (iv): next_legal_action differs between observer_look_before_act
    (record_work_mode_transition) and observer_execution_supervisor
    (dispatch_bounded_worker).  Tests the gate function directly.
    """
    # Look-before-act (default mode).
    look = observer_session.build_observer_root_route_context(
        backlog_id="AC-MODE-LBA-20260609",
        route_context=_build_full_route_context(),
        work_mode="observer_look_before_act",
    )
    assert look["next_legal_action"]["id"] == "record_work_mode_transition"

    # Execution supervisor mode — dispatcher gate should open.
    sup = observer_session.build_observer_root_route_context(
        backlog_id="AC-MODE-SUP-20260609",
        route_context=_build_full_route_context(),
        work_mode="observer_execution_supervisor",
    )
    assert sup["next_legal_action"]["id"] == "dispatch_bounded_worker"

    # The two modes must return different next_legal_action ids.
    assert look["next_legal_action"]["id"] != sup["next_legal_action"]["id"]

    # Supervisor mode must not block dispatch.
    assert "dispatch_implementation" not in sup["blocked_actions"]
    assert "dispatch_implementation" in sup["allowed_actions"]

    # Look-before-act must block supervisor actions until transition.
    assert "dispatch_implementation" in look["blocked_actions"]


# ---------------------------------------------------------------------------
# GAP 1 regression tests — POST handler binds caller-supplied trace id
# (server._is_plausible_graph_trace_id and POST body binding)
# ---------------------------------------------------------------------------


def test_is_plausible_graph_trace_id_accepts_valid_format():
    """The server-side plausibility check accepts gqt-YYYYMMDD-hex strings."""
    from agent.governance.server import _is_plausible_graph_trace_id

    assert _is_plausible_graph_trace_id("gqt-20260610-abc123ef90") is True
    assert _is_plausible_graph_trace_id("gqt-20260610-c2445c014f") is True
    assert _is_plausible_graph_trace_id("gqt-20260101-0000000000") is True


def test_is_plausible_graph_trace_id_rejects_invalid():
    """The plausibility check rejects non-gqt strings, blanks, and injections."""
    from agent.governance.server import _is_plausible_graph_trace_id

    assert _is_plausible_graph_trace_id("") is False
    assert _is_plausible_graph_trace_id(None) is False
    assert _is_plausible_graph_trace_id("trace-123") is False
    assert _is_plausible_graph_trace_id("gqt-2026-abc") is False          # 4-digit date
    assert _is_plausible_graph_trace_id("gqt-20260610-") is False          # empty hex
    assert _is_plausible_graph_trace_id("gqt-20260610-ABCDEF") is False    # uppercase hex
    assert _is_plausible_graph_trace_id("gqt-20260610-abc xyz") is False   # spaces
    assert _is_plausible_graph_trace_id("arbitrary-string") is False


def test_post_handler_binds_caller_trace_id_via_state_function():
    """GAP 1: _observer_root_route_context_state prefers a plausible caller-
    supplied graph_query_schema_trace_id over the (empty) timeline-derived one.
    Uses a minimal in-memory DB that has no timeline events.
    """
    import sqlite3
    from unittest.mock import patch
    from agent.governance import task_timeline, backlog_runtime
    from agent.governance.server import _observer_root_route_context_state
    from agent.governance import observer_session as obs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Ensure minimal schema so the state function can run.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backlog_bugs "
        "(bug_id TEXT PRIMARY KEY, chain_trigger_json TEXT)"
    )
    task_timeline.ensure_schema(conn)

    caller_trace = "gqt-20260610-c2445c014f"

    result = _observer_root_route_context_state(
        conn,
        "demo",
        backlog_id="AC-TRACE-BIND-TEST-20260609",
        work_mode=obs.normalize_work_mode(None),
        caller_graph_query_schema_trace_id=caller_trace,
    )

    # The caller-supplied plausible trace id must be echoed in the response.
    assert result["graph_query_schema_trace_id"] == caller_trace


def test_post_handler_ignores_implausible_caller_trace_id():
    """GAP 1: when the caller supplies an implausible trace id, the state
    function falls back to the timeline-derived value (empty when no events).
    """
    import sqlite3
    from agent.governance import task_timeline
    from agent.governance.server import _observer_root_route_context_state
    from agent.governance import observer_session as obs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backlog_bugs "
        "(bug_id TEXT PRIMARY KEY, chain_trigger_json TEXT)"
    )
    task_timeline.ensure_schema(conn)

    result = _observer_root_route_context_state(
        conn,
        "demo",
        backlog_id="AC-TRACE-BIND-IGNORE-TEST-20260609",
        work_mode=obs.normalize_work_mode(None),
        caller_graph_query_schema_trace_id="not-a-valid-trace-id",
    )

    # Implausible trace id must not be echoed — falls back to empty.
    assert result["graph_query_schema_trace_id"] == ""
