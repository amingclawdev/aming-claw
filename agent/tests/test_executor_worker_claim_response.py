import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from executor_worker import ExecutorWorker


def _worker():
    return ExecutorWorker(
        project_id="proj",
        governance_url="http://localhost:40000",
        worker_id="executor-test",
        workspace="C:/repo",
    )


def test_claim_task_accepts_current_dict_response_shape():
    worker = _worker()
    worker._api = MagicMock(return_value={
        "task": {
            "task_id": "task-current",
            "type": "pm",
            "prompt": "draft PRD",
            "metadata": {},
        },
        "fence_token": "fence-current",
    })

    task = worker._claim_task()

    assert task["task_id"] == "task-current"
    assert task["_fence_token"] == "fence-current"


def test_claim_task_accepts_legacy_pair_response_shape():
    worker = _worker()
    worker._api = MagicMock(return_value={
        "task": [
            {
                "task_id": "task-legacy",
                "type": "pm",
                "prompt": "draft PRD",
                "metadata": {},
            },
            "fence-legacy",
        ],
    })

    task = worker._claim_task()

    assert task["task_id"] == "task-legacy"
    assert task["_fence_token"] == "fence-legacy"
