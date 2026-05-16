"""Tests that docs/governance/manual-fix-sop.md R11 row documents the
trailer-priority chain anchor architecture (replaces the deprecated
DB POST /api/version-update flow).

PRD: OPT-BACKLOG-MF-SOP-R11-STALE-DB-VERSION-UPDATE-DEPRECATED.

Acceptance criteria pinned here (AC1..AC10) match the PM contract; failures
in CI surface drift between SOP doc and the runtime trailer-priority gate.
"""

from pathlib import Path

import pytest

# Resolve repo root relative to this test file:
# agent/tests/test_manual_fix_sop_r11_trailer.py -> repo root is ../../
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "governance" / "manual-fix-sop.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    """Read the manual-fix SOP document once for all tests."""
    assert DOC_PATH.exists(), f"Document not found: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc_lines(doc_text: str) -> list:
    return doc_text.splitlines()


def _r11_row(doc_lines: list) -> str:
    """Return the single markdown table row whose first cell is `| R11 |`."""
    for line in doc_lines:
        stripped = line.lstrip()
        if stripped.startswith("| R11 |"):
            return line
    raise AssertionError("R11 row not found in manual-fix-sop.md")


# --- AC1: 'deprecated_write_ignored' present in R11 row context ---

def test_ac1_deprecated_write_ignored_in_r11(doc_lines: list):
    row = _r11_row(doc_lines)
    assert "deprecated_write_ignored" in row, (
        "R11 row must mention `deprecated_write_ignored` to document that "
        "the deprecated POST /api/version-update API now no-ops."
    )


# --- AC2: Chain-Source-Stage trailer example present (case-sensitive) ---

def test_ac2_chain_source_stage_observer_hotfix(doc_text: str):
    assert "Chain-Source-Stage: observer-hotfix" in doc_text, (
        "Doc must include the canonical trailer example "
        "'Chain-Source-Stage: observer-hotfix' (case-sensitive)."
    )


# --- AC3: chain_trailer.get_chain_state primary source mention ---

def test_ac3_chain_trailer_get_chain_state_referenced(doc_text: str):
    assert "chain_trailer.get_chain_state" in doc_text, (
        "Doc must reference chain_trailer.get_chain_state as the primary "
        "chain_version source used by handle_version_check."
    )


# --- AC4: Working MF reference commit 0d4329d cited ---

def test_ac4_reference_commit_0d4329d(doc_text: str):
    assert "0d4329d" in doc_text, (
        "Doc must cite the working trailered MF commit 0d4329d as a reference."
    )


# --- AC5: 4-field commit-message template block ---

@pytest.mark.parametrize("field", [
    "Chain-Source-Task:",
    "Chain-Source-Stage:",
    "Chain-Parent:",
    "Chain-Bug-Id:",
])
def test_ac5_commit_message_template_fields(doc_text: str, field: str):
    assert field in doc_text, (
        f"Commit-message template must include the trailer field '{field}'."
    )


def test_ac5_commit_message_template_has_fenced_block(doc_text: str):
    """All four trailer fields must appear inside a single fenced code block."""
    in_fence = False
    fence_buf: list = []
    blocks: list = []
    for line in doc_text.splitlines():
        if line.startswith("```"):
            if in_fence:
                blocks.append("\n".join(fence_buf))
                fence_buf = []
                in_fence = False
            else:
                in_fence = True
            continue
        if in_fence:
            fence_buf.append(line)

    matching = [
        b for b in blocks
        if "Chain-Source-Task:" in b
        and "Chain-Source-Stage:" in b
        and "Chain-Parent:" in b
        and "Chain-Bug-Id:" in b
    ]
    assert matching, (
        "At least one fenced code block must contain all four trailer fields "
        "(Chain-Source-Task, Chain-Source-Stage, Chain-Parent, Chain-Bug-Id)."
    )


# --- AC6: Status banner on line 3 mentions current SOP version + trailer ---

def test_ac6_status_banner_current_version_trailer(doc_lines: list):
    assert len(doc_lines) >= 3, "Doc must have at least 3 lines for the banner"
    banner = doc_lines[2]
    assert "v7" in banner, (
        f"Line 3 status banner must mention 'v7'; got: {banner!r}"
    )
    assert "trailer" in banner.lower(), (
        f"Line 3 status banner must mention 'trailer'; got: {banner!r}"
    )


# --- AC7: R11 and R12 row identifiers preserved ---

def test_ac7_r11_row_present(doc_text: str):
    assert "| R11 |" in doc_text, "R11 row marker `| R11 |` must be preserved"


def test_ac7_r12_row_present(doc_text: str):
    assert "| R12 |" in doc_text, "R12 row marker `| R12 |` must be preserved"


# --- AC8: R11 Required Action does NOT recommend version-update as primary ---

def test_ac8_r11_does_not_recommend_version_update_as_primary(doc_lines: list):
    row = _r11_row(doc_lines)
    # Must mention 'deprecated' AND 'trailer' as the canonical mechanism
    assert "deprecated" in row, (
        "R11 row must contain the substring 'deprecated' to mark the legacy "
        "version-update path as deprecated."
    )
    assert "trailer" in row, (
        "R11 row must contain the substring 'trailer' to document the new "
        "trailer-priority mechanism."
    )


# --- AC10: Total occurrence count > 6 baseline ---

def test_ac10_trailer_keyword_density(doc_text: str):
    n = (
        doc_text.count("deprecated_write_ignored")
        + doc_text.count("trailer")
        + doc_text.count("observer-hotfix")
    )
    assert n > 6, (
        f"Expected union-count of trailer-priority keywords > 6, got {n}. "
        "Doc must document trailer-priority architecture in enough places to "
        "leave the deprecated DB-write path unambiguously secondary."
    )
