"""Spec lint test for docs/governance/reconcile-workflow.md.

Validates structural integrity: section references resolve, AC numbered,
failure modes documented, no orphan sections, §13 resolved.
"""

import pathlib
import re

import pytest

SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "governance" / "reconcile-workflow.md"


@pytest.fixture
def spec_content():
    assert SPEC_PATH.exists(), f"Spec file not found: {SPEC_PATH}"
    return SPEC_PATH.read_text(encoding="utf-8")


def test_minimum_length(spec_content):
    """AC1: Spec is >= 350 lines."""
    lines = spec_content.splitlines()
    assert len(lines) >= 350, f"Spec has {len(lines)} lines, expected >= 350"


def test_all_13_sections_present(spec_content):
    """AC2: All 13 section headers present."""
    for i in range(1, 14):
        header = f"## §{i}"
        assert header in spec_content, f"Missing section header: {header}"


def test_phase_sections_have_required_elements(spec_content):
    """AC3: Phase sections §3-§10 have required structural elements."""
    sections = re.split(r"^## §", spec_content, flags=re.MULTILINE)
    # sections[0] is before §1, sections[1] is §1 content, etc.
    for phase_num in range(3, 11):
        # Find the section content for this phase
        section_text = None
        for s in sections:
            if s.startswith(str(phase_num)):
                section_text = s
                break
        assert section_text is not None, f"§{phase_num} section not found"

        has_input = "Input contract" in section_text or "Trigger" in section_text
        assert has_input, f"§{phase_num} missing 'Input contract' or 'Trigger'"

        has_output = "Output contract" in section_text or "Acceptance criteria" in section_text
        assert has_output, f"§{phase_num} missing 'Output contract' or 'Acceptance criteria'"

        assert "Failure modes" in section_text, f"§{phase_num} missing 'Failure modes'"
        assert "Rollback path" in section_text, f"§{phase_num} missing 'Rollback path'"


def test_section2_task_definition(spec_content):
    """AC4: §2 contains creator/allowlist, audit, and rate limit."""
    sections = re.split(r"^## §", spec_content, flags=re.MULTILINE)
    section2 = None
    for s in sections:
        if s.startswith("2"):
            section2 = s
            break
    assert section2 is not None, "§2 not found"

    has_creator = "creator" in section2.lower() or "allowlist" in section2.lower()
    assert has_creator, "§2 missing 'creator' or 'allowlist'"
    assert "audit" in section2.lower(), "§2 missing 'audit'"
    has_rate = "rate limit" in section2.lower() or "Rate limit" in section2
    assert has_rate, "§2 missing 'rate limit'"


def test_gate_exemption_code_locations(spec_content):
    """AC5: §11 contains verified code locations."""
    assert "auto_chain.py:3923" in spec_content, "Missing _gate_release location"
    assert "auto_chain.py:3745" in spec_content, "Missing _gate_qa_pass location"
    assert "auto_chain.py:3610" in spec_content, "Missing _gate_t2_pass location"


def test_no_unresolved_questions(spec_content):
    """AC7: §13 has no unresolved questions."""
    sections = re.split(r"^## §", spec_content, flags=re.MULTILINE)
    section13 = None
    for s in sections:
        if s.startswith("13"):
            section13 = s
            break
    assert section13 is not None, "§13 not found"
    assert "Open question" not in section13, "§13 contains unresolved 'Open question'"
    assert "TODO" not in section13, "§13 contains 'TODO'"
    assert "TBD" not in section13, "§13 contains 'TBD'"


def test_meta_governance(spec_content):
    """AC8: Spec declares meta-governance."""
    assert "chain" in spec_content, "Missing 'chain' reference"
    has_meta = "meta-govern" in spec_content or "modify via chain" in spec_content
    assert has_meta, "Missing meta-governance declaration"


def test_cross_references(spec_content):
    """R6: Cross-references to existing governance docs."""
    assert "auto-chain.md" in spec_content, "Missing cross-ref to auto-chain.md"
    assert "version-control.md" in spec_content, "Missing cross-ref to version-control.md"
    assert "manual-fix-sop.md" in spec_content, "Missing cross-ref to manual-fix-sop.md"
