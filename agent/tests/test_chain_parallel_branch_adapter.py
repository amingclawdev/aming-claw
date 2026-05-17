"""PB-007 Chain adapter tests for parallel branch runtime identity."""

from __future__ import annotations

from agent.governance.chain_context import (
    build_parallel_branch_context_from_chain_payload,
    parallel_branch_event_payload_from_context,
)


def test_chain_parallel_branch_adapter_maps_nested_metadata_without_running_chain() -> None:
    payload = {
        "project_id": "fixture-project",
        "task_id": "chain-dev-2",
        "type": "dev",
        "parent_task_id": "chain-root-1",
        "metadata": {
            "bug_id": "ARCH-PB007",
            "chain_id": "chain-root-1",
            "parallel_branch": {
                "batch_id": "batch-PB007",
                "branch_ref": "refs/heads/codex/PB007-chain-dev",
                "ref_name": "main",
                "worktree_id": "wt-PB007",
                "worktree_path": "/tmp/worktrees/PB007-chain-dev",
                "base_commit": "B0",
                "head_commit": "H1",
                "target_head_commit": "M0",
                "snapshot_id": "scope-H1",
                "projection_id": "semproj-H1",
                "merge_queue_id": "mergeq-PB007",
                "merge_preview_id": "preview-PB007",
                "retry_round": 2,
                "depends_on": ["chain-test-1"],
                "checkpoint_id": "checkpoint-PB007",
                "replay_source": "checkpoint",
                "lease_id": "lease-PB007",
                "lease_expires_at": "2026-05-16T12:30:00Z",
                "fence_token": "fence-PB007",
            },
        },
    }

    context = build_parallel_branch_context_from_chain_payload(payload)

    assert context is not None
    assert context.project_id == "fixture-project"
    assert context.task_id == "chain-dev-2"
    assert context.chain_id == "chain-root-1"
    assert context.root_task_id == "chain-root-1"
    assert context.stage_task_id == "chain-dev-2"
    assert context.stage_type == "dev"
    assert context.retry_round == 2
    assert context.attempt == 3
    assert context.backlog_id == "ARCH-PB007"
    assert context.branch_ref == "refs/heads/codex/PB007-chain-dev"
    assert context.depends_on == ("chain-test-1",)
    assert context.merge_queue_id == "mergeq-PB007"
    assert context.fence_token == "fence-PB007"


def test_chain_parallel_branch_adapter_accepts_flat_metadata_and_emits_event_envelope() -> None:
    payload = {
        "project_id": "fixture-project",
        "task_id": "chain-qa-3",
        "type": "qa",
        "metadata": {
            "chain_id": "chain-root-2",
            "root_task_id": "chain-root-2",
            "bug_id": "ARCH-PB007-FLAT",
            "branch_ref": "refs/heads/codex/PB007-chain-qa",
            "worktree_path": "/tmp/worktrees/PB007-chain-qa",
            "target_head_commit": "M0",
            "retry_round": 1,
            "attempt": 4,
            "batch_id": "batch-PB007",
            "hard_depends_on": "chain-dev-2",
            "checkpoint_id": "checkpoint-qa",
            "lease_id": "lease-qa",
            "fence_token": "fence-qa",
            "merge_preview_id": "preview-qa",
            "rollback_epoch": "rollback-001",
            "replay_epoch": "replay-001",
        },
    }

    context = build_parallel_branch_context_from_chain_payload(payload)
    assert context is not None
    assert context.stage_type == "qa"
    assert context.retry_round == 1
    assert context.attempt == 4
    assert context.depends_on == ("chain-dev-2",)
    assert context.worktree_path == "/tmp/worktrees/PB007-chain-qa"
    assert context.target_head_commit == "M0"
    assert context.checkpoint_id == "checkpoint-qa"
    assert context.rollback_epoch == "rollback-001"
    assert context.replay_epoch == "replay-001"

    event = parallel_branch_event_payload_from_context(
        context,
        event_type="branch_task.checkpointed",
        actor="chain-worker",
        payload={"checkpoint_id": "checkpoint-qa"},
    )

    assert event["event_type"] == "branch_task.checkpointed"
    assert event["project_id"] == "fixture-project"
    assert event["chain_id"] == "chain-root-2"
    assert event["stage_task_id"] == "chain-qa-3"
    assert event["stage_type"] == "qa"
    assert event["attempt"] == 4
    assert event["branch_ref"] == "refs/heads/codex/PB007-chain-qa"
    assert event["worktree_path"] == "/tmp/worktrees/PB007-chain-qa"
    assert event["target_head_commit"] == "M0"
    assert event["merge_preview_id"] == "preview-qa"
    assert event["depends_on"] == ["chain-dev-2"]
    assert event["checkpoint_id"] == "checkpoint-qa"
    assert event["lease_id"] == "lease-qa"
    assert event["fence_token"] == "fence-qa"
    assert event["rollback_epoch"] == "rollback-001"
    assert event["actor"] == "chain-worker"
    assert event["payload"] == {"checkpoint_id": "checkpoint-qa"}
    assert event["created_at"]


def test_chain_parallel_branch_adapter_is_noop_without_branch_ref() -> None:
    payload = {
        "project_id": "fixture-project",
        "task_id": "serial-dev",
        "type": "dev",
        "metadata": {"chain_id": "serial-root"},
    }

    assert build_parallel_branch_context_from_chain_payload(payload) is None
