"""Guards for the parallel agent multibranch runtime design contract."""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "dev" / "parallel-agent-multibranch-design.md"

REQUIRED_SECTIONS = [
    "## Purpose",
    "## Non-Goals",
    "## Scenario Coverage",
    "## Runtime Surfaces",
    "## Canonical Identity",
    "## Event Envelope",
    "## BranchTaskRuntimeContext",
    "## MergeQueueRuntime",
    "## BatchMergeRuntime",
    "## Graph And Semantic Ref Rules",
    "## PendingScopeRuntime",
    "## Dashboard And MCP Read Model",
    "## Chain Compatibility",
    "## MVP Implementation Order",
    "## Pending Infrastructure",
    "## Acceptance Bar",
]

REQUIRED_IDENTITIES = [
    "project_id",
    "batch_id",
    "backlog_id",
    "task_id",
    "chain_id",
    "root_task_id",
    "stage_task_id",
    "stage_type",
    "agent_id",
    "worker_id",
    "attempt",
    "lease_id",
    "fence_token",
    "branch_ref",
    "ref_name",
    "worktree_id",
    "worktree_path",
    "base_commit",
    "head_commit",
    "target_head_commit",
    "snapshot_id",
    "projection_id",
    "merge_queue_id",
    "merge_preview_id",
    "rollback_epoch",
    "replay_epoch",
]

REQUIRED_RUNTIME_STATES = [
    "allocated",
    "running",
    "checkpointed",
    "scope_ready",
    "review_ready",
    "merge_queued",
    "merged",
    "batch_retained",
    "cleaned",
    "lease_expired",
    "reclaimable",
    "base_stale",
    "dependency_blocked",
    "merge_blocked",
    "rollback_required",
    "replay_pending",
    "stale_after_dependency_merge",
    "merge_failed",
]

REQUIRED_SCENARIOS = [f"PB-{index:03d}" for index in range(1, 13)]


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"Document not found: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def test_design_doc_exists() -> None:
    assert DOC_PATH.exists()


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_required_sections_are_documented(doc_text: str, section: str) -> None:
    assert section in doc_text


@pytest.mark.parametrize("identity", REQUIRED_IDENTITIES)
def test_required_identity_fields_are_documented(doc_text: str, identity: str) -> None:
    assert f"`{identity}`" in doc_text or f'"{identity}"' in doc_text


@pytest.mark.parametrize("state", REQUIRED_RUNTIME_STATES)
def test_required_runtime_states_are_documented(doc_text: str, state: str) -> None:
    assert state in doc_text


@pytest.mark.parametrize("scenario_id", REQUIRED_SCENARIOS)
def test_design_maps_to_test_scenarios(doc_text: str, scenario_id: str) -> None:
    assert scenario_id in doc_text


def test_design_points_to_test_scenario_matrix(doc_text: str) -> None:
    lower_text = doc_text.lower()
    assert "docs/dev/parallel-agent-multibranch-test-scenarios.md" in doc_text
    assert "test oracle" in lower_text
    assert "pending infrastructure flags" in doc_text


def test_design_preserves_non_mf_only_contract(doc_text: str) -> None:
    assert "Do not encode MF-only assumptions" in doc_text
    assert "Chain identity fields are optional but reserved" in doc_text
    assert "No API should assume all future clients are MF clients" in doc_text
