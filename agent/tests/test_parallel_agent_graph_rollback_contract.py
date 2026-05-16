"""Guards for the parallel graph/ref/DB rollback design contract."""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "dev" / "parallel-agent-graph-rollback-contract.md"

REQUIRED_SECTIONS = [
    "## Purpose",
    "## Scenario Coverage",
    "## Graph Ref Event Model",
    "## Operation Types",
    "## Currentness Rules",
    "## Rollback Epoch",
    "## Replay Epoch",
    "## Table Ownership",
    "## Governance Hint Rollback",
    "## Migration Strategy",
    "## API Requirements",
    "## Implementation Order",
    "## Acceptance Bar",
]

REQUIRED_OPERATIONS = [
    "activate",
    "merge",
    "rollback",
    "revert",
    "replay",
    "backfill_escape",
]

REQUIRED_TABLE_AREAS = [
    "graph_snapshot_refs",
    "graph_ref_events",
    "graph_snapshots",
    "graph_events",
    "graph_semantic_projections",
    "graph_semantic_nodes",
    "graph_semantic_edges",
    "graph_semantic_jobs",
    "pending_scope_reconcile",
    "Governance Hint source files",
]

REQUIRED_SCENARIOS = ["PB-004", "PB-005", "PB-006", "PB-011", "PB-012"]


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"Document not found: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def test_graph_rollback_contract_doc_exists() -> None:
    assert DOC_PATH.exists()


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_required_sections_are_documented(doc_text: str, section: str) -> None:
    assert section in doc_text


@pytest.mark.parametrize("operation", REQUIRED_OPERATIONS)
def test_ref_operations_are_documented(doc_text: str, operation: str) -> None:
    assert f"`{operation}`" in doc_text


@pytest.mark.parametrize("table_area", REQUIRED_TABLE_AREAS)
def test_table_ownership_is_documented(doc_text: str, table_area: str) -> None:
    assert table_area in doc_text


@pytest.mark.parametrize("scenario", REQUIRED_SCENARIOS)
def test_parallel_scenarios_are_mapped(doc_text: str, scenario: str) -> None:
    assert scenario in doc_text


def test_rollback_and_replay_epochs_are_first_class(doc_text: str) -> None:
    assert "`rollback_epoch`" in doc_text
    assert "`replay_epoch`" in doc_text
    assert "Rollback must be idempotent" in doc_text
    assert "Replay starts after rollback and uses retained branch heads" in doc_text


def test_abandoned_branch_semantics_cannot_be_current(doc_text: str) -> None:
    required_fragments = [
        "branch-local candidate snapshot that has not been merged",
        "merge epoch abandoned by rollback",
        "abandoned rows cannot be current",
        "candidate, stale, inactive, abandoned, or rebuild_required",
    ]
    for fragment in required_fragments:
        assert fragment in doc_text


def test_hint_rollback_requires_inverse_deltas(doc_text: str) -> None:
    for delta in ["hint_added", "hint_changed", "hint_removed", "hint_rollback_restored"]:
        assert f"`{delta}`" in doc_text
    assert "Rollback must not leave a stale binding" in doc_text
