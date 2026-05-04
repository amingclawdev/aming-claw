"""Tests for the reconcile-cluster role-prompt extensions.

Covers Reconcile CR4 acceptance criteria AC1-AC8: PM and Dev role prompts
in agent/role_permissions.py carry the reconcile-cluster contract, and
docs/roles/{pm,dev}.md mirror that contract for human-readable audit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.role_permissions import _DEFAULT_ROLE_PROMPTS  # noqa: E402


_PM_DOC = _REPO_ROOT / "docs" / "roles" / "pm.md"
_DEV_DOC = _REPO_ROOT / "docs" / "roles" / "dev.md"


def test_pm_role_prompt_mentions_reconcile_cluster_operation_type():
    """AC1 + AC2: PM prompt declares the reconcile-cluster contract.

    The PM Python-default prompt MUST contain the literal substring
    'reconcile-cluster', a 'Reconcile cluster audit' heading, the
    bootstrap-only directive 'MUST NOT declare removed_nodes', and
    instruct PM to preserve concrete candidate node ids from
    metadata.cluster_payload/metadata.cluster_report.
    """
    pm_prompt = _DEFAULT_ROLE_PROMPTS["pm"]
    assert "reconcile-cluster" in pm_prompt, (
        "PM prompt missing literal 'reconcile-cluster'"
    )
    assert "Reconcile cluster audit" in pm_prompt, (
        "PM prompt missing 'Reconcile cluster audit' heading"
    )
    assert "metadata.cluster_payload" in pm_prompt, (
        "PM prompt must instruct reading metadata.cluster_payload"
    )
    assert "metadata.cluster_report" in pm_prompt, (
        "PM prompt must instruct reading metadata.cluster_report"
    )
    assert "copy that node_id exactly" in pm_prompt, (
        "PM prompt must preserve concrete candidate node ids"
    )


def test_pm_prompt_forbids_removed_nodes_for_reconcile_cluster():
    """AC1 (bootstrap rule): PM prompt forbids removed_nodes/unmapped_files
    declarations under reconcile-cluster (always-bootstrap mode)."""
    pm_prompt = _DEFAULT_ROLE_PROMPTS["pm"]
    assert "MUST NOT declare removed_nodes" in pm_prompt, (
        "PM prompt missing literal 'MUST NOT declare removed_nodes'"
    )
    # Same paragraph should also forbid unmapped_files (always-bootstrap).
    assert "unmapped_files" in pm_prompt, (
        "PM prompt should reference unmapped_files in the bootstrap rule"
    )
    # PM doc mirror — pm.md must carry the reconcile heading + a fenced
    # JSON code block + the operation_type token (AC6).
    pm_md = _PM_DOC.read_text(encoding="utf-8")
    assert "## Reconcile cluster audit pattern" in pm_md, (
        "docs/roles/pm.md missing '## Reconcile cluster audit pattern' heading"
    )
    assert "operation_type" in pm_md, (
        "docs/roles/pm.md missing 'operation_type' substring"
    )
    # Fenced JSON block following the heading.
    heading_idx = pm_md.index("## Reconcile cluster audit pattern")
    after_heading = pm_md[heading_idx:]
    # Stop at the next top-level heading so we only consider the section's body.
    next_heading = after_heading.find("\n## ", 1)
    section_body = after_heading if next_heading == -1 else after_heading[:next_heading]
    assert "```json" in section_body, (
        "docs/roles/pm.md '## Reconcile cluster audit pattern' section "
        "missing fenced JSON code block"
    )


def test_dev_role_prompt_covers_reconcile_cluster_doc_test_responsibility():
    """AC4 + AC5 + AC7: Dev prompt + dev doc carry the reconcile-cluster
    contract (Reconcile cluster work heading, graph_delta.creates directive,
    doc_impact + test_files responsibilities)."""
    dev_prompt = _DEFAULT_ROLE_PROMPTS["dev"]
    assert "reconcile-cluster" in dev_prompt, (
        "Dev prompt missing literal 'reconcile-cluster'"
    )
    assert "Reconcile cluster work" in dev_prompt, (
        "Dev prompt missing 'Reconcile cluster work' heading"
    )
    assert "graph_delta.creates" in dev_prompt, (
        "Dev prompt missing literal 'graph_delta.creates'"
    )
    assert "doc_impact" in dev_prompt, (
        "Dev prompt must instruct dev to update doc_impact for reconcile-cluster"
    )
    assert "test_files" in dev_prompt, (
        "Dev prompt must instruct dev to update test_files for reconcile-cluster"
    )
    # Dev doc mirror — dev.md '## Task Workflow' section must mention all
    # three tokens (AC7).
    dev_md = _DEV_DOC.read_text(encoding="utf-8")
    workflow_idx = dev_md.index("## Task Workflow")
    next_heading = dev_md.find("\n## ", workflow_idx + 1)
    workflow_body = (
        dev_md[workflow_idx:] if next_heading == -1 else dev_md[workflow_idx:next_heading]
    )
    assert "reconcile-cluster" in workflow_body, (
        "docs/roles/dev.md '## Task Workflow' missing 'reconcile-cluster'"
    )
    assert "graph_delta.creates" in workflow_body, (
        "docs/roles/dev.md '## Task Workflow' missing 'graph_delta.creates'"
    )
    assert "doc_impact" in workflow_body, (
        "docs/roles/dev.md '## Task Workflow' missing 'doc_impact'"
    )


def test_pm_prompt_includes_concrete_example():
    """AC3: PM prompt embeds a concrete example block referencing
    operation_type + reconcile-cluster so the AI sees a worked payload."""
    pm_prompt = _DEFAULT_ROLE_PROMPTS["pm"]
    assert "Example" in pm_prompt, (
        "PM prompt missing the literal token 'Example'"
    )
    assert "operation_type" in pm_prompt, (
        "PM prompt missing 'operation_type' in the example block"
    )
    assert "reconcile-cluster" in pm_prompt, (
        "PM prompt missing 'reconcile-cluster' in the example block"
    )
    # Sanity: the three tokens should appear close together (within the
    # same example paragraph) — verify by checking that 'operation_type'
    # appears AFTER the 'Example' token in the prompt string.
    example_idx = pm_prompt.index("Example")
    op_idx = pm_prompt.index("operation_type", example_idx)
    rc_idx = pm_prompt.index("reconcile-cluster", example_idx)
    assert op_idx > example_idx and rc_idx > example_idx, (
        "Example block must contain operation_type and reconcile-cluster "
        "after the 'Example' token"
    )
