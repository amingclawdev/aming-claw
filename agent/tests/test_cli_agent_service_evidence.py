"""Tests for public-safe CLI Agent Service lifecycle receipts."""

import pytest

from agent.cli_agent_service.evidence import (
    CliAgentRunReceipt,
    RunReceiptEmitter,
    RunReceiptJournal,
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
        "process_start_identity_hash": hash_text("pid:1234:start:99"),
    }


def test_run_receipt_is_deterministic_public_safe_and_round_trips():
    receipt = CliAgentRunReceipt(
        **_identity(),
        state="completed",
        event_index=2,
        observed_at="2026-07-12T12:00:02Z",
        process_identity=_process_identity(),
        output_hash=hash_text("output"),
        duration_ms=1250,
        metering={"input_tokens": 12, "output_tokens": 4},
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


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("run_id", "run-private-prompt"),
        ("profile_id", "profile-secret-token"),
        ("observed_at", "private prompt body"),
        ("failure_category", "credential-like-text"),
    ],
)
def test_run_receipt_rejects_private_or_unstructured_public_text(
    field_name,
    value,
):
    values = {
        **_identity(),
        "state": "accepted",
        "event_index": 0,
        "observed_at": "2026-07-12T12:00:00Z",
    }
    values[field_name] = value

    with pytest.raises(ValueError):
        CliAgentRunReceipt(**values)


def test_run_receipt_rejects_raw_process_identity_and_unknown_fields():
    with pytest.raises(ValueError, match="raw process_start_identity"):
        CliAgentRunReceipt(
            **_identity(),
            state="started",
            event_index=1,
            observed_at="2026-07-12T12:00:01Z",
            process_identity={"process_start_identity": "private prompt body"},
        )
    accepted = CliAgentRunReceipt(
        **_identity(),
        state="accepted",
        event_index=0,
        observed_at="2026-07-12T12:00:00Z",
    ).to_public_dict()
    accepted["private_prompt"] = "must not persist"
    with pytest.raises(ValueError, match="unsupported fields"):
        CliAgentRunReceipt.from_public_dict(accepted)


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


def test_run_receipt_journal_is_durable_and_idempotent(tmp_path):
    journal = RunReceiptJournal(tmp_path / "receipts")
    receipt = CliAgentRunReceipt(
        **_identity(),
        state="accepted",
        event_index=0,
        observed_at="2026-07-12T12:00:00Z",
    ).to_public_dict()

    journal.append(receipt)
    journal.append(receipt)

    restarted = RunReceiptJournal(tmp_path / "receipts")
    assert restarted.receipts(receipt["run_id"]) == (receipt,)
    assert restarted.latest(receipt["run_id"]) == receipt


@pytest.mark.parametrize(
    "terminal_state,exit_code,failure_category",
    [
        ("failed", 1, "process_error"),
        ("cancelled", 130, "cancelled"),
        ("lost", None, "lost"),
    ],
)
def test_emitter_supports_non_success_terminal_states(
    terminal_state,
    exit_code,
    failure_category,
):
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
        exit_code=exit_code,
        failure_category=failure_category,
    )

    assert emitted[-1]["state"] == terminal_state
    assert emitted[-1]["governance_authority"] is False
