import os
import sys
from unittest import mock


agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


def test_scope_materialization_dev_prompt_preserves_pm_doc_impact():
    from governance.auto_chain import _build_dev_prompt

    pm_docs = [
        "docs/governance/reconcile-workflow.md",
        "docs/governance/auto-chain.md",
        "docs/governance/acceptance-graph.md",
    ]
    graph_docs = [
        "docs/api/governance-api.md",
        "docs/architecture.md",
        "docs/governance/reconcile-workflow.md",
    ]
    result = {
        "target_files": ["agent/governance/auto_chain.py"],
        "requirements": ["R1"],
        "acceptance_criteria": ["AC1"],
        "verification": {"command": "python -m pytest agent/tests/test_qa_graph_delta_review.py -q"},
        "doc_impact": {"files": list(pm_docs), "changes": ["PM-scoped docs only"]},
    }
    metadata = {
        "project_id": "aming-claw",
        "operation_type": "scope-materialization",
        "target_files": ["agent/governance/auto_chain.py"],
    }

    with mock.patch("governance.auto_chain._get_task_graph_doc_associations", return_value=graph_docs):
        _, out_meta = _build_dev_prompt("task-pm", result, metadata)

    assert out_meta["doc_impact"]["files"] == pm_docs
    assert "docs/api/governance-api.md" not in out_meta["doc_impact"]["files"]
    assert "docs/architecture.md" not in out_meta["doc_impact"]["files"]


def test_regular_dev_prompt_still_merges_graph_docs():
    from governance.auto_chain import _build_dev_prompt

    result = {
        "target_files": ["agent/governance/auto_chain.py"],
        "requirements": ["R1"],
        "acceptance_criteria": ["AC1"],
        "verification": {"command": "python -m pytest agent/tests/test_qa_graph_delta_review.py -q"},
        "doc_impact": {"files": ["docs/governance/auto-chain.md"], "changes": ["PM doc"]},
    }
    metadata = {
        "project_id": "aming-claw",
        "operation_type": "feature",
        "target_files": ["agent/governance/auto_chain.py"],
    }

    with mock.patch(
        "governance.auto_chain._get_task_graph_doc_associations",
        return_value=["docs/api/governance-api.md"],
    ):
        _, out_meta = _build_dev_prompt("task-pm", result, metadata)

    assert set(out_meta["doc_impact"]["files"]) == {
        "docs/governance/auto-chain.md",
        "docs/api/governance-api.md",
    }


def test_scope_materialization_checkpoint_honors_explicit_pm_docs_only():
    from governance.auto_chain import _gate_checkpoint

    pm_docs = [
        "docs/governance/reconcile-workflow.md",
        "docs/governance/auto-chain.md",
        "docs/governance/acceptance-graph.md",
    ]
    result = {"changed_files": list(pm_docs)}
    metadata = {
        "operation_type": "scope-materialization",
        "target_files": ["agent/governance/auto_chain.py"],
        "doc_impact": {"files": list(pm_docs), "changes": ["PM-scoped docs only"]},
    }

    with mock.patch("governance.auto_chain._try_verify_update"), mock.patch(
        "governance.auto_chain._get_task_graph_doc_associations",
        return_value=["docs/api/governance-api.md", "docs/architecture.md"],
    ):
        passed, reason = _gate_checkpoint(None, "aming-claw", result, metadata)

    assert passed, reason
