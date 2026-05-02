"""Regression tests for deploy task expected_head propagation."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def test_executor_worker_passes_merge_commit_as_expected_head():
    """Deploy worker must not call run_deploy with an empty expected_head."""
    from executor_worker import ExecutorWorker

    worker = ExecutorWorker(
        "aming-claw",
        governance_url="http://localhost:40000",
        workspace=os.getcwd(),
    )
    metadata = {
        "changed_files": ["agent/executor_worker.py"],
        "merge_commit": "deadbeef",
    }
    report = {
        "success": True,
        "affected_services": ["executor"],
        "smoke_test": {
            "executor": True,
            "governance": "not_applicable",
            "gateway": "not_applicable",
            "all_pass": True,
        },
    }

    with mock.patch.object(worker, "_report_progress"), \
         mock.patch("deploy_chain.run_deploy", return_value=report) as mock_run:
        out = worker._execute_deploy("task-deploy-expected-head", metadata)

    assert out["status"] == "succeeded"
    mock_run.assert_called_once_with(
        ["agent/executor_worker.py"],
        chat_id=0,
        project_id="aming-claw",
        task_id="task-deploy-expected-head",
        expected_head="deadbeef",
    )


def test_executor_worker_does_not_use_parent_chain_version_as_expected_head():
    """A stale inherited chain_version must not overwrite the deployed HEAD."""
    from executor_worker import ExecutorWorker

    worker = ExecutorWorker(
        "aming-claw",
        governance_url="http://localhost:40000",
        workspace=os.getcwd(),
    )
    metadata = {
        "changed_files": ["agent/executor_worker.py"],
        "chain_version": "parent123",
    }
    report = {
        "success": True,
        "affected_services": ["executor"],
        "smoke_test": {
            "executor": True,
            "governance": "not_applicable",
            "gateway": "not_applicable",
            "all_pass": True,
        },
    }
    git_head = SimpleNamespace(returncode=0, stdout="head456\n", stderr="")

    with mock.patch.object(worker, "_report_progress"), \
         mock.patch("executor_worker.subprocess.run", return_value=git_head), \
         mock.patch("deploy_chain.run_deploy", return_value=report) as mock_run:
        out = worker._execute_deploy("task-deploy-head-fallback", metadata)

    assert out["status"] == "succeeded"
    assert mock_run.call_args.kwargs["expected_head"] == "head456"
