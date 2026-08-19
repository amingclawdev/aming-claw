"""Tests for backlog insert AI triage gate."""
import os, sys, types, importlib, importlib.abc, re as _re
_agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _agent_dir)

class _Py39Fix(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    _GOV = os.path.join(_agent_dir, "governance")
    _PAT = _re.compile(r"->\s*\w+\s*\|")
    _paths = {}
    def find_module(self, name, path=None):
        if name.startswith("governance.") and name not in sys.modules:
            fp = os.path.join(self._GOV, name.split(".")[-1] + ".py")
            if os.path.isfile(fp):
                with open(fp) as f: c = f.read()
                if "from __future__ import annotations" not in c and self._PAT.search(c):
                    self._paths[name] = fp; return self
    def load_module(self, name):
        if name in sys.modules: return sys.modules[name]
        with open(self._paths[name]) as f: src = "from __future__ import annotations\n" + f.read()
        m = types.ModuleType(name); m.__file__ = self._paths[name]; m.__package__ = "governance"; m.__path__ = []
        sys.modules[name] = m; exec(compile(src, self._paths[name], "exec"), m.__dict__); return m
if sys.version_info < (3, 10): sys.meta_path.insert(0, _Py39Fix())

import json
from unittest.mock import MagicMock, patch
import pytest
from governance.backlog_triage import triage_backlog_insert

def _ctx(bug_id="NEW-1", pid="test-proj", **b):
    c = MagicMock(); c.path_params = {"project_id": pid, "bug_id": bug_id}; c.body = b; return c

def _conn(rows):
    c = MagicMock()
    def _ex(sql, params=None):
        r = MagicMock()
        if "SELECT" in str(sql) and "status='OPEN'" in str(sql):
            r.fetchall.return_value = rows
        elif "SELECT * FROM backlog_bugs WHERE bug_id" in str(sql):
            selected_id = (params or [""])[0]
            matched = next(
                (dict(row) for row in rows if row.get("bug_id") == selected_id),
                None,
            )
            if matched is not None:
                matched.setdefault("status", "OPEN")
                matched.setdefault("bypass_policy_json", "{}")
            r.fetchone.return_value = matched
        else:
            r.fetchone.return_value = None
            r.fetchall.return_value = []
        return r
    c.execute.side_effect = _ex; return c

@pytest.fixture(autouse=True)
def _aud():
    with patch("governance.server.audit_service"): yield

def test_admit_when_no_open_rows():
    with patch("governance.server.get_connection", return_value=_conn([])):
        from governance.server import handle_backlog_upsert
        assert handle_backlog_upsert(_ctx(title="New bug"))["ok"] is True

def test_admit_when_no_overlap():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "X", "title": "Other", "target_files": '["z.py"]'}])):
        from governance.server import handle_backlog_upsert
        assert handle_backlog_upsert(_ctx(title="Different"))["ok"] is True

def test_supersede_requires_observer_decision():
    connection = _conn([{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}])
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="New", target_files=["a.py"]))
        assert isinstance(r, tuple) and r[0] == 409
        assert r[1]["error"] == "triage_review_required"
        assert r[1]["recommended_action"] == "supersede"
        assert r[1]["triage"]["evidence"]["candidates"][0]["bug_id"] == "OLD-2"
        assert r[1]["field"] == "triage_action"
        assert r[1]["expected"] == ["admit", "supersede", "reject_dup"]
        assert r[1]["actual"] == ""
        assert r[1]["guide"]["force_admit_required"] is False
        assert r[1]["guide"]["bypass_or_waive_required"] is False
        assert r[1]["source"].endswith("backlog_triage_prewrite_gate")
        assert r[1]["zero_write_rejection"] is True
        assert r[1]["writes_performed"] is False
        audit_record.assert_not_called()
        connection.commit.assert_not_called()

def test_confirmed_supersede_marks_old_row_superseded_not_fixed():
    conn = _conn([{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}])
    with patch("governance.server.get_connection", return_value=conn):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(
            title="New",
            target_files=["a.py"],
            triage_action="supersede",
            triage_target_bug_id="OLD-2",
            actor="observer",
        ))
        assert r["action"] == "superseded" and "OLD-2" in r["closed_bugs"]
        update_sql = [str(call.args[0]) for call in conn.execute.call_args_list]
        assert any("status='SUPERSEDED'" in sql for sql in update_sql)
        assert not any("status='FIXED'" in sql for sql in update_sql)
        assert r["atomic"] is True
        update_index = next(
            index
            for index, call in enumerate(conn.mock_calls)
            if call.args and "status='SUPERSEDED'" in str(call.args[0])
        )
        commit_index = next(
            index
            for index, call in enumerate(conn.mock_calls)
            if str(call).startswith("call.commit(")
        )
        assert update_index < commit_index


def test_force_admit_with_triage_decision_rejects_zero_write():
    connection = _conn(
        [{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}]
    )
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert

        response = handle_backlog_upsert(
            _ctx(
                title="New",
                target_files=["a.py"],
                force_admit=True,
                triage_action="supersede",
                triage_target_bug_id="OLD-2",
            )
        )

        assert response[0] == 409
        assert response[1]["error"] == "force_admit_triage_conflict"
        assert response[1]["field"] == "force_admit"
        assert response[1]["zero_write_rejection"] is True
        assert response[1]["writes_performed"] is False
        assert not any(
            call.args and "INSERT INTO backlog_bugs" in str(call.args[0])
            for call in connection.execute.call_args_list
        )
        audit_record.assert_not_called()
        connection.commit.assert_not_called()


@pytest.mark.parametrize(
    "body,error",
    [
        ({"force_admit": True, "triage_action": ""}, "force_admit_triage_conflict"),
        ({"triage_target_bug_id": "OLD-2"}, "triage_action_target_shape_invalid"),
        ({"triage_action": "supersede"}, "triage_action_target_shape_invalid"),
        (
            {"triage_action": "admit", "triage_target_bug_id": "OLD-2"},
            "triage_action_target_shape_invalid",
        ),
    ],
)
def test_triage_action_target_shape_rejects_zero_write(body, error):
    connection = _conn(
        [{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}]
    )
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert

        response = handle_backlog_upsert(_ctx(title="New", target_files=["a.py"], **body))

        assert response[0] == 409
        assert response[1]["error"] == error
        assert response[1]["zero_write_rejection"] is True
        assert response[1]["writes_performed"] is False
        assert not any(
            call.args and "INSERT INTO backlog_bugs" in str(call.args[0])
            for call in connection.execute.call_args_list
        )
        audit_record.assert_not_called()
        connection.commit.assert_not_called()


def _stored_backlog_row(bug_id, *, status="OPEN", policy=None, **updates):
    row = {
        "bug_id": bug_id,
        "title": "New",
        "status": status,
        "priority": "P3",
        "target_files": '["a.py"]',
        "test_files": "[]",
        "acceptance_criteria": "[]",
        "chain_task_id": "",
        "commit": "",
        "discovered_at": "",
        "fixed_at": "",
        "details_md": "",
        "chain_trigger_json": "{}",
        "required_docs": "[]",
        "provenance_paths": "[]",
        "bypass_policy_json": json.dumps(policy or {}),
        "mf_type": "",
        "takeover_json": "{}",
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
    }
    row.update(updates)
    return row


def _exact_retry_rows(*, target_status="SUPERSEDED"):
    from governance import server

    successor = _stored_backlog_row("NEW-1")
    identity = server._backlog_triage_successor_identity(
        bug_id="NEW-1",
        body={},
        existing_row=successor,
    )
    binding = server._backlog_triage_supersede_binding(
        bug_id="NEW-1",
        target_bug_ids=["OLD-2"],
        successor_identity=identity,
    )
    successor["bypass_policy_json"] = json.dumps(
        {server._BACKLOG_TRIAGE_SUPERSEDE_BINDING_KEY: binding}
    )
    target_policy = {}
    if target_status == "SUPERSEDED":
        target_policy[server._BACKLOG_TRIAGE_SUPERSEDED_BY_BINDING_KEY] = {
            **binding,
            "target_bug_id": "OLD-2",
        }
    target = _stored_backlog_row(
        "OLD-2",
        title="Old",
        status=target_status,
        policy=target_policy,
    )
    return successor, target, binding


def _retry_connection(successor, target, *, fail_target_update=False):
    connection = MagicMock()

    def _execute(sql, params=None):
        text = str(sql)
        result = MagicMock()
        if "SELECT * FROM backlog_bugs WHERE bug_id" in text:
            selected_id = (params or [""])[0]
            result.fetchone.return_value = (
                successor if selected_id == "NEW-1" else target
            )
            return result
        if fail_target_update and "status='SUPERSEDED'" in text:
            raise RuntimeError("injected target update failure")
        result.fetchone.return_value = None
        result.fetchall.return_value = []
        return result

    connection.execute.side_effect = _execute
    return connection


def test_exact_supersede_retry_already_applied_is_read_only():
    successor, target, binding = _exact_retry_rows()
    connection = _retry_connection(successor, target)
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert

        response = handle_backlog_upsert(
            _ctx(
                triage_action="supersede",
                triage_target_bug_id="OLD-2",
            )
        )

        assert response["action"] == "superseded"
        assert response["atomic"] is True
        assert response["idempotent_retry"] is True
        assert response["already_applied"] is True
        assert response["writes_performed"] is False
        assert response["supersede_binding"] == binding
        assert not any(
            call.args
            and any(token in str(call.args[0]) for token in ("INSERT", "UPDATE"))
            for call in connection.execute.call_args_list
        )
        audit_record.assert_not_called()
        connection.commit.assert_not_called()


def test_exact_supersede_retry_completes_open_target_once():
    successor, target, binding = _exact_retry_rows(target_status="OPEN")
    connection = _retry_connection(successor, target)
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ):
        from governance.server import handle_backlog_upsert

        response = handle_backlog_upsert(
            _ctx(
                triage_action="supersede",
                triage_target_bug_id="OLD-2",
            )
        )

        assert response["action"] == "superseded"
        assert response["atomic"] is True
        assert response["idempotent_retry"] is True
        assert response["already_applied"] is False
        assert response["supersede_binding"] == binding
        assert any(
            call.args and "status='SUPERSEDED'" in str(call.args[0])
            for call in connection.execute.call_args_list
        )
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()


def test_supersede_retry_rejects_changed_successor_payload_zero_write():
    successor, target, _ = _exact_retry_rows()
    connection = _retry_connection(successor, target)
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert

        response = handle_backlog_upsert(
            _ctx(
                title="Forged changed successor",
                triage_action="supersede",
                triage_target_bug_id="OLD-2",
            )
        )

        assert response[0] == 409
        assert response[1]["error"] == "triage_supersede_retry_identity_mismatch"
        assert response[1]["zero_write_rejection"] is True
        assert not any(
            call.args
            and any(token in str(call.args[0]) for token in ("INSERT", "UPDATE"))
            for call in connection.execute.call_args_list
        )
        audit_record.assert_not_called()
        connection.commit.assert_not_called()


def test_supersede_insert_and_target_update_roll_back_together():
    connection = _conn(
        [{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}]
    )
    original_execute = connection.execute.side_effect

    def _execute(sql, params=None):
        if "status='SUPERSEDED'" in str(sql):
            raise RuntimeError("injected target update failure")
        return original_execute(sql, params)

    connection.execute.side_effect = _execute
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert

        with pytest.raises(RuntimeError, match="injected target update failure"):
            handle_backlog_upsert(
                _ctx(
                    title="New",
                    target_files=["a.py"],
                    triage_action="supersede",
                    triage_target_bug_id="OLD-2",
                    actor="observer",
                )
            )

        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        audit_record.assert_not_called()

def test_reject_dup_returns_409():
    connection = _conn([{"bug_id": "OLD-1", "title": "Dup Bug", "target_files": "[]"}])
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="Dup Bug"))
        assert isinstance(r, tuple) and r[0] == 409 and "duplicate_of" in r[1]
        assert r[1]["field"] == "triage_action"
        assert r[1]["zero_write_rejection"] is True
        assert r[1]["writes_performed"] is False
        audit_record.assert_not_called()
        connection.commit.assert_not_called()


@pytest.mark.parametrize(
    ("body", "error", "field"),
    [
        (
            {
                "title": "New",
                "target_files": ["a.py"],
                "triage_action": "force_admit",
            },
            "invalid_triage_action",
            "triage_action",
        ),
        (
            {
                "title": "New",
                "target_files": ["a.py"],
                "triage_action": "supersede",
                "triage_target_bug_id": "NOT-A-CANDIDATE",
            },
            "triage_target_not_candidate",
            "triage_target_bug_id",
        ),
    ],
)
def test_triage_input_rejections_share_host_correctable_zero_write_diagnostic(
    body,
    error,
    field,
):
    connection = _conn(
        [{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}]
    )
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert

        response = handle_backlog_upsert(_ctx(**body))

        assert response[0] in {400, 409}
        diagnostic = response[1]
        assert diagnostic["error"] == error
        assert diagnostic["field"] == field
        assert {
            "expected",
            "actual",
            "guide",
            "source",
            "zero_write_rejection",
            "writes_performed",
        }.issubset(diagnostic)
        assert diagnostic["zero_write_rejection"] is True
        assert diagnostic["writes_performed"] is False
        assert diagnostic["guide"]["force_admit_required"] is False
        assert diagnostic["guide"]["bypass_or_waive_required"] is False
        audit_record.assert_not_called()
        connection.commit.assert_not_called()

def test_merge_into_requires_observer_decision():
    connection = _conn([{"bug_id": "OLD-3", "title": "O", "target_files": '["a.py","b.py","c.py"]'}])
    with patch("governance.server.get_connection", return_value=connection), patch(
        "governance.server.audit_service.record"
    ) as audit_record:
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="X", target_files=["a.py", "b.py"], details_md="e"))
        assert isinstance(r, tuple) and r[0] == 409
        assert r[1]["error"] == "triage_review_required"
        assert r[1]["recommended_action"] == "merge_into"
        assert r[1]["triage"]["evidence"]["candidates"][0]["bug_id"] == "OLD-3"
        assert {
            "field",
            "expected",
            "actual",
            "guide",
            "source",
            "zero_write_rejection",
            "writes_performed",
        }.issubset(r[1])
        assert r[1]["zero_write_rejection"] is True
        assert r[1]["writes_performed"] is False
        audit_record.assert_not_called()
        connection.commit.assert_not_called()

def test_confirmed_merge_into_appends_details():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "OLD-3", "title": "O", "target_files": '["a.py","b.py","c.py"]'}])):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(
            title="X",
            target_files=["a.py", "b.py"],
            details_md="e",
            triage_action="merge_into",
            triage_target_bug_id="OLD-3",
            actor="observer",
        ))
        assert r["action"] == "merge_into" and r["bug_id"] == "OLD-3"

def test_single_generic_file_overlap_does_not_merge_unrelated_domains():
    decision = triage_backlog_insert(
        {
            "bug_id": "BUG-AUDIT-GRAPH-QUERY-TRACE-LEFT-RUNNING-R1-20260518",
            "title": "Graph query audit traces remain running after one-shot MCP/API queries",
            "target_files": [
                "agent/governance/graph_query_trace.py",
                "agent/governance/server.py",
                "agent/governance/mcp_server.py",
            ],
        },
        [
            {
                "bug_id": "OPT-FILE-INVENTORY-ORPHAN-LIST-PERFORMANCE",
                "title": "File inventory orphan queries need timeout and pagination hardening",
                "target_files": '["agent/governance/server.py","agent/governance/graph_snapshot_store.py","agent/governance/reconcile_file_inventory.py"]',
            }
        ],
    )

    assert decision["action"] == "admit"

def test_single_context_file_same_target_does_not_supersede_distinct_rows():
    decision = triage_backlog_insert(
        {
            "bug_id": "MS-RENDER-E2E-VIDEO-EVIDENCE-20260531",
            "title": "Content production render verification should expose dashboard evidence",
            "target_files": ["content-system/render-pipeline.md"],
        },
        [
            {
                "bug_id": "MS-GRAPH-FIXTURE-BINDINGS-20260531",
                "title": "Graph fixture bindings should cover render docs",
                "target_files": '["content-system/render-pipeline.md"]',
            }
        ],
    )

    assert decision["action"] == "admit"

def test_single_context_file_with_weak_title_overlap_does_not_merge():
    decision = triage_backlog_insert(
        {
            "bug_id": "AC-RUNTIME-STATUS-SM-GOV-VERSION-MISMATCH-20260531",
            "title": "Runtime status compact explanation for service mismatch",
            "target_files": ["agent/governance/server.py", "agent/governance/runtime_status.py"],
        },
        [
            {
                "bug_id": "OPT-GOVERNANCE-HINT-ATTACH-COMPACT-RESPONSE",
                "title": "Governance hint compact response should stay readable",
                "target_files": '["agent/governance/server.py","agent/governance/hints.py"]',
            }
        ],
    )

    assert decision["action"] == "admit"

def test_common_context_patterns_do_not_block_single_file_rows():
    rows = [
        {
            "bug_id": "DOC-README-BINDING-20260531",
            "title": "Graph fixture binding uses README examples",
            "target_files": '["README.md"]',
        },
        {
            "bug_id": "SCRIPT-MCP-STABLE-DECISION-20260531",
            "title": "Stable decision dedupe script should keep compact output",
            "target_files": '["scripts/example_mcp.py"]',
        },
    ]

    readme_decision = triage_backlog_insert(
        {
            "bug_id": "MS-DEMO-README-20260531",
            "title": "Marketing demo requirements should mention reviewer evidence",
            "target_files": ["README.md"],
        },
        [rows[0]],
    )
    script_decision = triage_backlog_insert(
        {
            "bug_id": "EXT-MCP-VOICE-FOLLOWUP-20260531",
            "title": "Voice conversion follow-up should register command help",
            "target_files": ["scripts/example_mcp.py"],
        },
        [rows[1]],
    )

    assert readme_decision["action"] == "admit"
    assert script_decision["action"] == "admit"

def test_single_context_file_with_strong_title_match_still_flags_candidate():
    decision = triage_backlog_insert(
        {
            "bug_id": "MS-RENDER-PIPELINE-E2E-20260531",
            "title": "Render pipeline E2E evidence is missing",
            "target_files": ["content-system/render-pipeline.md"],
        },
        [
            {
                "bug_id": "MS-RENDER-PIPELINE-VIDEO-20260531",
                "title": "Render pipeline E2E video evidence is stale",
                "target_files": '["content-system/render-pipeline.md"]',
            }
        ],
    )

    assert decision["action"] == "supersede"
    assert decision["evidence"]["title_token_overlap"] == ["e2e", "evidence", "pipeline", "render"]

def test_explicit_backlog_lineage_allows_single_context_file_candidate():
    decision = triage_backlog_insert(
        {
            "bug_id": "MS-RENDER-CHILD-20260531",
            "title": "Child follow-up should track video output",
            "target_files": ["content-system/render-pipeline.md", "content-system/video.md"],
            "details_md": "Follow-up for MS-GRAPH-FIXTURE-BINDINGS-20260531.",
        },
        [
            {
                "bug_id": "MS-GRAPH-FIXTURE-BINDINGS-20260531",
                "title": "Graph fixture bindings should cover render docs",
                "target_files": '["content-system/render-pipeline.md","content-system/fixtures.md"]',
            }
        ],
    )

    assert decision["action"] == "merge_into"
    assert decision["evidence"]["lineage_match"] is True

def test_merge_into_reports_overlap_evidence():
    decision = triage_backlog_insert(
        {"bug_id": "NEW-1", "title": "Worker queue lease timeout", "target_files": ["a.py", "b.py"]},
        [{"bug_id": "OLD-1", "title": "Worker queue stale lease", "target_files": '["a.py","z.py"]'}],
    )

    assert decision["action"] == "merge_into"
    assert decision["evidence"]["overlap_files"] == ["a.py"]
    assert decision["evidence"]["title_token_overlap"] == ["lease", "queue", "worker"]
    assert decision["evidence"]["candidates"][0]["bug_id"] == "OLD-1"

def test_force_admit_bypasses_gate():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "OLD-1", "title": "Dup Bug", "target_files": "[]"}])):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="Dup Bug", force_admit=True))
        assert r["ok"] is True and r["action"] == "upserted"

def test_agent_failure_falls_back_to_admit():
    c = MagicMock()
    def _ex(sql, params=None):
        if "SELECT" in str(sql) and "status='OPEN'" in str(sql): raise RuntimeError("boom")
        r = MagicMock(); r.fetchone.return_value = None; return r
    c.execute.side_effect = _ex
    with patch("governance.server.get_connection", return_value=c):
        from governance.server import handle_backlog_upsert
        assert handle_backlog_upsert(_ctx(title="Bug"))["ok"] is True
