"""Tests that docs/governance/manual-fix-sop.md exists and contains all
required sections per PRD OPT-BACKLOG-MANUAL-FIX-BOUNDARY-DOC."""

from pathlib import Path

import pytest

# Resolve repo root relative to this test file:
# agent/tests/test_manual_fix_doc_present.py -> repo root is ../../
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "governance" / "manual-fix-sop.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    """Read the manual-fix SOP document once for all tests."""
    assert DOC_PATH.exists(), f"Document not found: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


# --- AC-MF1: Doc exists ---

def test_doc_exists():
    assert DOC_PATH.exists(), f"Expected {DOC_PATH} to exist"


# --- AC-MF2: Decision matrix with 7 rows ---

def test_decision_matrix_section(doc_text: str):
    assert "## Decision matrix" in doc_text


@pytest.mark.parametrize("row", [
    "Missing API doc",
    "Missing unit test",
    "Phase Z found candidate node missing",
    "Stale ref / wrong path",
    "Chain pipeline itself broken",
    "Security/sensitive doc",
    "Hotfix during incident",
])
def test_decision_matrix_rows(doc_text: str, row: str):
    assert row in doc_text, f"Decision matrix missing row: {row}"


# --- AC-MF3: Why manual-fix is narrow ---

def test_why_narrow_section(doc_text: str):
    assert "## Why manual-fix is narrow" in doc_text


@pytest.mark.parametrize("mechanism", [
    "PM PRD review",
    "dev contract",
    "test/qa",
    "gatekeeper",
    "audit trail",
])
def test_why_narrow_mechanisms(doc_text: str, mechanism: str):
    assert mechanism in doc_text, f"Narrowness section missing mechanism: {mechanism}"


# --- AC-MF4: B48 precedent ---

def test_b48_precedent(doc_text: str):
    assert "B48 precedent" in doc_text


# --- AC-MF5: Manual-fix has only ---

def test_manual_fix_has_only_section(doc_text: str):
    assert "## Manual-fix has only" in doc_text


def test_manual_fix_prefix(doc_text: str):
    assert "manual fix:" in doc_text


def test_observer_hotfix_prefix(doc_text: str):
    assert "[observer-hotfix]" in doc_text


# --- AC-MF6: Concrete manual-fix authorship steps ---

def test_authorship_steps_section(doc_text: str):
    assert "## Concrete manual-fix authorship steps" in doc_text


# --- AC-MF7: Cross-references ---

def test_cross_references_section(doc_text: str):
    assert "## Cross-references" in doc_text


def test_cross_references_proposal(doc_text: str):
    assert "proposal-reconcile-comprehensive-2026-04-25.md" in doc_text


# --- AC-MF5 (extra): Examples section ---

def test_examples_section(doc_text: str):
    assert "## Examples" in doc_text
