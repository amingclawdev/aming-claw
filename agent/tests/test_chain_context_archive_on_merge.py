"""Regression tests for archiving chain context at merge completion."""

from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.chain_context import ChainContextStore


def test_merge_completion_persists_completed_then_archives():
    store = ChainContextStore()
    events = []

    def record(root_task_id, task_id, event_type, payload, project_id, conn=None):
        events.append((root_task_id, task_id, event_type, payload, project_id))

    store._persist_event = record

    store.on_task_created({
        "task_id": "pm-1",
        "type": "pm",
        "prompt": "plan",
        "parent_task_id": "",
        "project_id": "proj",
    })
    store.on_task_created({
        "task_id": "merge-1",
        "type": "merge",
        "prompt": "merge",
        "parent_task_id": "pm-1",
        "project_id": "proj",
    })

    store.on_task_completed({
        "task_id": "merge-1",
        "type": "merge",
        "project_id": "proj",
        "result": {"merge_commit": "abc123"},
    })

    assert store.get_chain("pm-1") is None
    assert store.get_chain("merge-1") is None
    assert [e[2] for e in events][-2:] == ["task.completed", "chain.archived"]
    completed = events[-2]
    archived = events[-1]
    assert completed[0] == "pm-1"
    assert completed[1] == "merge-1"
    assert completed[3]["result"] == {}
    assert archived[0] == "pm-1"
    assert archived[1] == "merge-1"
    assert archived[3]["root_task_id"] == "pm-1"


def test_recovery_releases_previously_completed_merge_without_db_write():
    store = ChainContextStore()
    events = []

    def record_unless_recovering(*args, **kwargs):
        if not store._recovering:
            events.append(args)

    store._persist_event = record_unless_recovering
    store._recovering = True

    store.on_task_created({
        "task_id": "merge-root",
        "type": "merge",
        "prompt": "merge",
        "parent_task_id": "",
        "project_id": "proj",
    })
    store.on_task_completed({
        "task_id": "merge-root",
        "type": "merge",
        "project_id": "proj",
        "result": {"merge_commit": "abc123"},
    })

    assert store.get_chain("merge-root") is None
    assert events == []
