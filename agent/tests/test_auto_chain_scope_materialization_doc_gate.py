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


def test_scope_materialization_graph_delta_only_checkpoint_can_advance():
    from governance.auto_chain import _gate_checkpoint

    result = {
        "summary": "Docs already exist; graph_delta materializes doc assets.",
        "changed_files": [],
        "test_results": {
            "ran": True,
            "passed": 1,
            "failed": 0,
            "command": "python -c \"print('ok')\"",
        },
        "graph_delta": {
            "creates": [
                {
                    "parent_layer": 7,
                    "title": "Governance documentation index",
                    "primary": [],
                    "secondary": [
                        "docs/governance/README.md",
                        "docs/roles/gatekeeper.md",
                    ],
                    "test_coverage": "doc_assertion",
                }
            ],
            "updates": [],
            "links": [],
        },
    }
    metadata = {
        "operation_type": "scope-materialization",
        "target_files": ["docs/governance/README.md", "docs/roles/gatekeeper.md"],
    }

    passed, reason = _gate_checkpoint(None, "aming-claw", result, metadata)

    assert passed is True
    assert reason == "scope-materialization graph_delta-only accepted"


def test_scope_materialization_checkpoint_accepts_existing_docs_attached_by_graph_delta():
    from governance.auto_chain import _gate_checkpoint

    expected_docs = [
        "docs/governance/audit-process.md",
        "examples/external-governance-demo/README.md",
        "examples/external-governance-demo/docs/usage.md",
    ]
    result = {
        "summary": "Mapped scope drift to graph nodes and fixed fixture test import.",
        "changed_files": ["examples/external-governance-demo/tests/test_service.py"],
        "test_results": {
            "ran": True,
            "passed": 17,
            "failed": 0,
            "command": "python -m pytest agent/tests/test_batch_jobs.py",
        },
        "graph_delta": {
            "creates": [
                {
                    "title": "External project governance scanner fixture",
                    "primary": [
                        "agent/governance/external_project_governance.py",
                        "examples/external-governance-demo/src/demo_app/service.py",
                    ],
                    "secondary": expected_docs[1:],
                    "test": ["examples/external-governance-demo/tests/test_service.py"],
                }
            ],
            "updates": [
                {
                    "node_id": "L7.6",
                    "fields": {
                        "secondary": [expected_docs[0]],
                        "test": ["agent/tests/test_cli.py"],
                    },
                }
            ],
            "links": [],
        },
    }
    metadata = {
        "operation_type": "scope-materialization",
        "target_files": [
            "examples/external-governance-demo/tests/test_service.py",
            *expected_docs,
        ],
        "doc_impact": {"files": list(expected_docs), "changes": ["Attach existing docs to graph nodes"]},
    }

    passed, reason = _gate_checkpoint(None, "aming-claw", result, metadata)

    assert passed is True, reason


def test_regular_checkpoint_still_requires_doc_edits_even_if_graph_delta_mentions_docs():
    from governance.auto_chain import _gate_checkpoint

    result = {
        "summary": "Regular code fix.",
        "changed_files": ["src/app.py"],
        "test_results": {"ran": True, "passed": 1, "failed": 0},
        "graph_delta": {
            "creates": [],
            "updates": [
                {
                    "node_id": "L7.1",
                    "fields": {"secondary": ["docs/app.md"]},
                }
            ],
            "links": [],
        },
    }
    metadata = {
        "operation_type": "feature",
        "target_files": ["src/app.py"],
        "doc_impact": {"files": ["docs/app.md"]},
    }

    passed, reason = _gate_checkpoint(None, "aming-claw", result, metadata)

    assert passed is False
    assert "Related docs not updated" in reason


def test_scope_materialization_graph_delta_only_failed_tests_block():
    from governance.auto_chain import _gate_checkpoint

    result = {
        "summary": "Docs already exist; graph_delta materializes doc assets.",
        "changed_files": [],
        "test_results": {
            "ran": True,
            "passed": 0,
            "failed": 1,
            "command": "python -c \"raise AssertionError('missing docs')\"",
        },
        "graph_delta": {
            "creates": [
                {
                    "parent_layer": 7,
                    "title": "Doc node",
                    "secondary": ["docs/a.md"],
                }
            ],
            "updates": [],
            "links": [],
        },
    }
    metadata = {
        "operation_type": "scope-materialization",
        "target_files": ["docs/a.md"],
    }

    passed, reason = _gate_checkpoint(None, "aming-claw", result, metadata)

    assert passed is False
    assert "scope-materialization verification failed with 1 failing tests" in reason


def test_scope_materialization_qa_prompt_scopes_global_release_gate():
    from governance.auto_chain import _build_qa_prompt

    result = {
        "test_report": {"tool": "pytest", "passed": 1, "failed": 0, "errors": 0},
        "changed_files": ["docs/governance/auto-chain.md"],
    }
    metadata = {
        "project_id": "aming-claw",
        "operation_type": "scope-materialization",
        "target_files": ["docs/governance/auto-chain.md"],
        "acceptance_criteria": ["AC1: scoped docs updated"],
        "doc_impact": {"files": ["docs/governance/auto-chain.md"], "changes": ["scoped"]},
    }

    with mock.patch("governance.auto_chain._query_graph_delta_proposed", return_value=None), mock.patch(
        "governance.auto_chain._get_task_graph_doc_associations",
        return_value=[],
    ):
        prompt, _ = _build_qa_prompt("task-test", result, metadata)

    assert "Scope Materialization QA Scope" in prompt
    assert "Do not call or require the global /api/wf/{project_id}/release-gate" in prompt
    assert "MUST NOT set recommendation='reject'" in prompt


def test_scope_materialization_qa_prompt_includes_dev_doc_debt_context():
    from governance.auto_chain import _build_qa_prompt, _build_test_prompt

    dev_result = {
        "summary": "Recorded absent scratch doc as doc_debt.",
        "changed_files": [],
        "test_results": {"ran": True, "passed": 1, "failed": 0},
        "retry_context": {"is_retry": True, "fix_applied": "Added doc_debt."},
        "doc_debt": [
            {
                "path": "docs/dev/scratch/reconcile-comprehensive-2026-05-06.md",
                "reason": "Absent scratch record; not durable graph-owned docs.",
            }
        ],
    }
    pm_metadata = {
        "project_id": "aming-claw",
        "operation_type": "scope-materialization",
        "target_files": ["docs/governance/README.md"],
        "acceptance_criteria": [
            "AC4: Dev output explicitly records scratch docs as doc_debt.",
        ],
        "doc_impact": {"files": ["docs/governance/README.md"]},
    }

    _, test_meta = _build_test_prompt("task-dev", dev_result, pm_metadata)

    assert test_meta["dev_doc_debt"] == dev_result["doc_debt"]

    test_result = {
        "test_report": {"tool": "pytest", "passed": 1, "failed": 0, "errors": 0},
        "changed_files": [],
    }
    with mock.patch("governance.auto_chain._query_graph_delta_proposed", return_value=None):
        prompt, qa_meta = _build_qa_prompt("task-test", test_result, test_meta)

    assert qa_meta["test_report"] == test_result["test_report"]
    assert "Scope Materialization Dev Audit Context" in prompt
    assert "dev_doc_debt" in prompt
    assert "docs/dev/scratch/reconcile-comprehensive-2026-05-06.md" in prompt
    assert "Test may carry only test_report and changed_files" in prompt


def test_scope_materialization_test_prompt_carries_graph_delta_doc_debt_waivers():
    from governance.auto_chain import _build_test_prompt

    waiver = {
        "path": "docs/dev/scratch/reconcile-comprehensive-2026-05-06.md",
        "status": "waived",
        "reason": "Absent scratch record; not durable graph-owned docs.",
    }
    dev_result = {
        "summary": "Recorded absent scratch doc as graph_delta doc_debt_waivers.",
        "changed_files": [],
        "test_results": {"ran": True, "passed": 1, "failed": 0},
        "graph_delta": {
            "creates": [],
            "updates": [],
            "links": [],
            "doc_debt_waivers": [waiver],
        },
    }
    pm_metadata = {
        "project_id": "aming-claw",
        "operation_type": "scope-materialization",
        "target_files": ["docs/governance/README.md"],
    }

    _, test_meta = _build_test_prompt("task-dev", dev_result, pm_metadata)

    assert test_meta["dev_doc_debt"] == [waiver]


def test_existing_graph_node_create_is_normalized_to_update():
    from governance.auto_chain import _normalize_existing_node_creates

    fake_graph = mock.Mock()
    fake_graph.list_nodes.return_value = ["L7.23", "L7.47"]
    delta = {
        "creates": [
            {
                "node_id": "L7.23",
                "title": "Auto-chain",
                "primary": ["agent/governance/auto_chain.py"],
                "test": ["agent/tests/test_auto_chain_scope_materialization_doc_gate.py"],
            },
            {
                "node_id": None,
                "title": "New scope catchup wrapper",
                "primary": ["agent/governance/reconcile_scope_catchup.py"],
            },
        ],
        "updates": [],
        "links": [],
    }

    with mock.patch("governance.project_service.load_project_graph", return_value=fake_graph):
        normalized, moved = _normalize_existing_node_creates("aming-claw", delta)

    assert moved == 1
    assert [c.get("title") for c in normalized["creates"]] == ["New scope catchup wrapper"]
    assert normalized["updates"] == [
        {
            "node_id": "L7.23",
            "fields": {
                "title": "Auto-chain",
                "primary": ["agent/governance/auto_chain.py"],
                "test": ["agent/tests/test_auto_chain_scope_materialization_doc_gate.py"],
            },
            "normalized_from_create": True,
        }
    ]
