"""Tests for public-safe CLI Agent Service lifecycle receipts."""

import pytest

from agent.cli_agent_service.evidence import (
    CliAgentRunReceipt,
    RunReceiptEmitter,
    hash_text,
)


def _identity():
    return {
        "run_id": "run-receipt-a",
        "ticket_id": "caet-1234567890abcdef12345678",
        "ticket_hash": hash_text("ticket"),
        "profile_id": "codex-profile-a",
        "runtime_context_id": "mfrctx-run-receipt-a",
        "command_hash": hash_text("command"),
    }


def _process_identity():
    return {
        "pid": 1234,
        "process_group_id": 1234,
        "process_start_identity": "pid:1234:start:99",
    }


def test_run_receipt_is_deterministic_public_safe_and_round_trips():
    receipt = CliAgentRunReceipt(
        **_identity(),
        state="completed",
        event_index=2,
        observed_at="2026-07-12T12:00:02Z",
        process_identity={**_process_identity(), "environment": "secret"},
        output_hash=hash_text("output"),
        duration_ms=1250,
        metering={"input_tokens": 12, "output_tokens": 4, "provider_blob": "raw"},
        exit_code=0,
    )

    public = receipt.to_public_dict()
    assert public == receipt.to_public_dict()
    assert public["receipt_id"].startswith("clirct-")
    assert public["receipt_hash"].startswith("sha256:")
    assert public["process_identity"] == _process_identity()
    assert public["metering"] == {"input_tokens": 12, "output_tokens": 4}
    assert public["raw_prompt_stored"] is False
    assert public["raw_output_stored"] is False
    assert public["governance_authority"] is False
    assert CliAgentRunReceipt.from_public_dict(public).to_public_dict() == public


def test_run_receipt_rejects_content_tampering():
    receipt = CliAgentRunReceipt(
        **_identity(),
        state="accepted",
        event_index=0,
        observed_at="2026-07-12T12:00:00Z",
    ).to_public_dict()
    receipt["profile_id"] = "another-profile"

    with pytest.raises(ValueError, match="does not match"):
        CliAgentRunReceipt.from_public_dict(receipt)


def test_emitter_produces_contiguous_lifecycle_and_rejects_post_terminal_fact():
    emitted = []
    emitter = RunReceiptEmitter(**_identity(), sink=emitted.append)

    emitter.emit("accepted", observed_at="2026-07-12T12:00:00Z")
    emitter.emit(
        "started",
        observed_at="2026-07-12T12:00:01Z",
        process_identity=_process_identity(),
    )
    emitter.emit(
        "heartbeat",
        observed_at="2026-07-12T12:00:02Z",
        process_identity=_process_identity(),
    )
    emitter.emit(
        "completed",
        observed_at="2026-07-12T12:00:03Z",
        process_identity=_process_identity(),
        output_hash=hash_text("output"),
        duration_ms=3000,
        metering={"input_tokens": 10, "output_tokens": 2},
        exit_code=0,
    )

    assert [item["state"] for item in emitted] == [
        "accepted",
        "started",
        "heartbeat",
        "completed",
    ]
    assert [item["event_index"] for item in emitted] == [0, 1, 2, 3]
    with pytest.raises(ValueError, match="terminal"):
        emitter.emit(
            "heartbeat",
            observed_at="2026-07-12T12:00:04Z",
            process_identity=_process_identity(),
        )


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled", "lost"])
def test_emitter_supports_non_success_terminal_states(terminal_state):
    emitted = []
    emitter = RunReceiptEmitter(**_identity(), sink=emitted.append)
    emitter.emit("accepted", observed_at="2026-07-12T12:00:00Z")
    emitter.emit(
        "started",
        observed_at="2026-07-12T12:00:01Z",
        process_identity=_process_identity(),
    )
    emitter.emit(
        terminal_state,
        observed_at="2026-07-12T12:00:02Z",
        process_identity=_process_identity(),
        output_hash=hash_text("output"),
        duration_ms=2000,
        exit_code=1,
        failure_category=terminal_state,
    )

    assert emitted[-1]["state"] == terminal_state
    assert emitted[-1]["governance_authority"] is False
