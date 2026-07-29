from __future__ import annotations

import sqlite3

from agent.governance import auto_chain


_TARGET_FILES_UNSET = object()


def _pm_result(*, criteria, target_files=_TARGET_FILES_UNSET):
    return {
        "target_files": (
            ["agent/governance/auto_chain.py"]
            if target_files is _TARGET_FILES_UNSET
            else target_files
        ),
        "test_files": ["agent/tests/test_auto_chain.py"],
        "acceptance_criteria": criteria,
        "verification": {"commands": ["pytest agent/tests/test_auto_chain.py"]},
        "proposed_nodes": ["governance.auto_chain"],
        "doc_impact": {"files": [], "changes": ["No docs required."]},
        "skip_reasons": {},
    }


def test_post_pm_gate_blocks_free_text_acceptance_before_dev_dispatch(monkeypatch):
    monkeypatch.setattr(
        auto_chain,
        "_get_task_graph_doc_associations",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        auto_chain,
        "_get_graph_related_nodes",
        lambda *_args, **_kwargs: [],
    )
    conn = sqlite3.connect(":memory:")
    result = _pm_result(criteria=["PG-002 behavior is fixed"])

    passed, reason = auto_chain._gate_post_pm(
        conn,
        "aming-claw",
        result,
        {"task_id": "pm-pg002"},
    )

    assert passed is False
    assert "acceptance scope is not closed" in reason
    assert result["acceptance_scope_closure"]["errors"] == [
        "acceptance_criteria_require_stable_ids",
        "acceptance_criteria_require_structured_required_scope",
    ]


def test_post_pm_gate_blocks_required_file_missing_from_dev_fence(monkeypatch):
    monkeypatch.setattr(
        auto_chain,
        "_get_task_graph_doc_associations",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        auto_chain,
        "_get_graph_related_nodes",
        lambda *_args, **_kwargs: [],
    )
    conn = sqlite3.connect(":memory:")
    result = _pm_result(
        criteria=[
            {
                "id": "AC-PG002",
                "required_scope": {
                    "kind": "files",
                    "files": [
                        "agent/governance/auto_chain.py",
                        "agent/governance/server.py",
                    ],
                },
            }
        ]
    )

    passed, reason = auto_chain._gate_post_pm(
        conn,
        "aming-claw",
        result,
        {"task_id": "pm-pg002"},
    )

    assert passed is False
    assert "agent/governance/server.py" in reason
    assert result["acceptance_scope_closure"]["missing_required_files"] == [
        "agent/governance/server.py"
    ]


def test_post_pm_gate_accepts_files_nodes_and_external_verification(monkeypatch):
    monkeypatch.setattr(
        auto_chain,
        "_get_task_graph_doc_associations",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        auto_chain,
        "_get_graph_related_nodes",
        lambda *_args, **_kwargs: [],
    )
    conn = sqlite3.connect(":memory:")
    result = _pm_result(
        criteria=[
            {
                "id": "AC-CODE",
                "required_scope": {
                    "kind": "files_and_nodes",
                    "files": ["agent/governance/auto_chain.py"],
                    "node_ids": ["governance.auto_chain"],
                },
            },
            {
                "id": "AC-BROWSER",
                "required_scope": {
                    "kind": "verification_only_external_dependency",
                    "dependency_id": "browser:e2e",
                },
            },
        ]
    )

    passed, _reason = auto_chain._gate_post_pm(
        conn,
        "aming-claw",
        result,
        {"task_id": "pm-closed"},
    )

    assert passed is True
    assert result["acceptance_scope_closure"]["accepted"] is True
    assert result["acceptance_scope_closure"][
        "verification_only_external_dependencies"
    ] == ["browser:e2e"]


def test_post_pm_gate_does_not_replace_explicit_empty_fence_from_metadata(
    monkeypatch,
):
    monkeypatch.setattr(
        auto_chain,
        "_get_task_graph_doc_associations",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        auto_chain,
        "_get_graph_related_nodes",
        lambda *_args, **_kwargs: [],
    )
    conn = sqlite3.connect(":memory:")
    result = _pm_result(
        target_files=[],
        criteria=[
            {
                "id": "AC-PM-EXPLICIT-EMPTY",
                "required_scope": {
                    "kind": "files",
                    "files": ["agent/governance/auto_chain.py"],
                },
            }
        ],
    )

    passed, reason = auto_chain._gate_post_pm(
        conn,
        "aming-claw",
        result,
        {
            "task_id": "pm-explicit-empty",
            "target_files": ["agent/governance/auto_chain.py"],
        },
    )

    assert passed is False
    assert reason == "PRD target_files is empty"
