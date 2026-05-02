from __future__ import annotations

import json
import sqlite3

from agent.governance import backlog_runtime


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE backlog_bugs (
             bug_id TEXT PRIMARY KEY,
             chain_task_id TEXT DEFAULT '',
             chain_stage TEXT DEFAULT '',
             stage_updated_at TEXT DEFAULT '',
             last_failure_reason TEXT DEFAULT '',
             runtime_state TEXT DEFAULT '',
             current_task_id TEXT DEFAULT '',
             root_task_id TEXT DEFAULT '',
             worktree_path TEXT DEFAULT '',
             worktree_branch TEXT DEFAULT '',
             bypass_policy_json TEXT DEFAULT '{}',
             mf_type TEXT DEFAULT '',
             takeover_json TEXT DEFAULT '{}',
             runtime_updated_at TEXT DEFAULT '',
             updated_at TEXT DEFAULT ''
           )"""
    )
    return conn


def test_update_backlog_runtime_persists_task_worktree_and_policy():
    conn = _make_conn()
    conn.execute("INSERT INTO backlog_bugs (bug_id) VALUES ('BUG-1')")

    backlog_runtime.update_backlog_runtime(
        conn,
        "BUG-1",
        "dev_complete",
        task_id="task-dev",
        task_type="dev",
        root_task_id="task-root",
        metadata={
            "bypass_graph_governance": True,
            "skip_reason": "manual fix audit path",
        },
        result={
            "_worktree": ".worktrees/dev-task-dev",
            "_branch": "dev/task-dev",
        },
    )

    row = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='BUG-1'").fetchone()
    assert row["runtime_state"] == "in_chain"
    assert row["current_task_id"] == "task-dev"
    assert row["root_task_id"] == "task-root"
    assert row["worktree_path"] == ".worktrees/dev-task-dev"
    assert row["worktree_branch"] == "dev/task-dev"
    policy = json.loads(row["bypass_policy_json"])
    assert policy["graph_governance"] == "bypass"
    assert policy["skip_reason"] == "manual fix audit path"


def test_merge_policy_into_metadata_sets_graph_bypass_flags():
    metadata = backlog_runtime.merge_policy_into_metadata(
        {"bug_id": "BUG-1"},
        {"graph_governance": "bypass", "bypass_reason": "MF reconcile repair"},
    )

    assert backlog_runtime.is_graph_governance_bypassed(metadata) is True
    assert metadata["bypass_graph_governance"] is True
    assert metadata["skip_graph_delta_validation"] is True
    assert metadata["skip_reason"] == "MF reconcile repair"


def test_chain_rescue_policy_keeps_graph_governance_enforced():
    policy = backlog_runtime.build_mf_policy(
        "chain_rescue",
        mf_id="MF-2026-05-02-002",
        observer_authorized=True,
        reason="observer rescue",
    )

    assert policy["mf_type"] == "chain_rescue"
    assert policy["graph_governance"] == "enforce"
    assert policy["bypass_graph_governance"] is False
    assert backlog_runtime.is_graph_governance_bypassed({"backlog_bypass_policy": policy}) is False


def test_system_recovery_policy_bypasses_graph_governance():
    policy = backlog_runtime.build_mf_policy(
        "system_recovery",
        mf_id="MF-2026-05-02-002",
        observer_authorized=True,
        reason="repair governance runtime",
    )

    assert policy["mf_type"] == "system_recovery"
    assert policy["graph_governance"] == "bypass"
    assert policy["bypass_graph_governance"] is True
    assert backlog_runtime.is_graph_governance_bypassed({"backlog_bypass_policy": policy}) is True
