"""Reconcile-cluster checkpoint gate no-op audit regression tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_reconcile_cluster_noop_audit_can_advance_to_test(isolated_gov_db):
    from governance.auto_chain import _gate_checkpoint

    passed, reason = _gate_checkpoint(
        isolated_gov_db,
        "aming-claw",
        {
            "summary": "Audited existing LanguageAdapter contract; no code changes required.",
            "changed_files": [],
            "test_results": {
                "ran": True,
                "passed": 7,
                "failed": 0,
                "command": "pytest agent/tests/test_language_adapters.py -v",
            },
        },
        {
            "operation_type": "reconcile-cluster",
            "target_files": ["agent/governance/language_adapters/base.py"],
        },
    )

    assert passed is True
    assert reason == "reconcile-cluster no-op audit accepted"


def test_reconcile_cluster_noop_with_failed_tests_gets_actionable_reason(isolated_gov_db):
    from governance.auto_chain import _gate_checkpoint

    passed, reason = _gate_checkpoint(
        isolated_gov_db,
        "aming-claw",
        {
            "summary": "Audit found failing schema tests but made no changes.",
            "changed_files": [],
            "test_results": {
                "ran": True,
                "passed": 105,
                "failed": 2,
                "command": "pytest agent/tests/test_baseline_service.py -v",
            },
        },
        {
            "operation_type": "reconcile-cluster",
            "target_files": ["agent/governance/db.py"],
        },
    )

    assert passed is False
    assert "verification failed with 2 failing tests" in reason
    assert "fix the allowed source/doc/test files" in reason


def test_empty_non_reconcile_dev_result_still_blocks(isolated_gov_db):
    from governance.auto_chain import _gate_checkpoint

    passed, reason = _gate_checkpoint(
        isolated_gov_db,
        "aming-claw",
        {
            "summary": "No changes.",
            "changed_files": [],
            "test_results": {"ran": True, "passed": 1, "failed": 0},
        },
        {"target_files": ["agent/foo.py"]},
    )

    assert passed is False
    assert reason == "No files changed"
