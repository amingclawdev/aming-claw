"""Tests for task implementation timeline evidence."""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _conn(tmp_dir):
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from agent.governance.db import get_connection

    return get_connection("proj")


def _ctx(query=None, *, path_params=None, body=None, method="GET"):
    from agent.governance import server

    params = {"project_id": "proj"}
    if path_params:
        params.update(path_params)
    return server.RequestContext(
        None,
        method,
        params,
        query or {},
        body or {},
        "req-test",
        "",
        "",
    )


class TestTaskTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_concurrent_timeline_writes_use_serialized_queue(self):
        from agent.governance import task_timeline

        errors = []

        def write(i):
            try:
                task_timeline.enqueue_event(
                    "proj",
                    task_id="task-concurrent",
                    backlog_id="BUG-TL",
                    attempt_num=1,
                    event_type="ai.implementation_evidence.proposed",
                    actor=f"worker-{i}",
                    status="proposed",
                    payload={"i": i},
                    wait=True,
                )
            except Exception as exc:  # pragma: no cover - failure surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        events = task_timeline.list_events(self.conn, "proj", task_id="task-concurrent")
        self.assertEqual(len(events), 20)
        self.assertEqual(
            {event["payload"]["i"] for event in events},
            set(range(20)),
        )

    def test_task_claim_and_complete_write_verified_timeline(self):
        from agent.governance import task_timeline
        from agent.governance.task_registry import claim_task, complete_task, create_task

        task = create_task(
            self.conn,
            "proj",
            "implement evidence",
            task_type="dev",
            metadata={"bug_id": "BUG-TL", "mf_id": "MF-TL", "trace_id": "tr-tl"},
        )
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1", caller_pid=1234)
        self.conn.commit()
        self.assertEqual(claimed["task_id"], task["task_id"])

        result = {
            "changed_files": ["agent/example.py"],
            "implementation_evidence": [
                {
                    "file": "agent/example.py",
                    "symbols": ["do_work"],
                    "change_intent": "add observable evidence",
                }
            ],
            "self_check": {
                "ready_for_gate": True,
                "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
            },
            "_artifacts": {"output_path": "shared-volume/codex-tasks/logs/output.txt"},
        }

        with mock.patch("agent.governance.auto_chain.on_task_completed", return_value=None):
            complete_task(
                self.conn,
                task["task_id"],
                status="succeeded",
                result=result,
                project_id="proj",
                completed_by="worker-1",
                fence_token=fence,
            )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", task_id=task["task_id"])
        event_types = [event["event_type"] for event in events]
        self.assertIn("task.claimed", event_types)
        self.assertIn("gate.evidence.verified", event_types)
        self.assertIn("task.completed", event_types)

        gate_event = next(event for event in events if event["event_type"] == "gate.evidence.verified")
        self.assertEqual(gate_event["status"], "passed")
        self.assertTrue(gate_event["verification"]["passed"])
        self.assertEqual(gate_event["backlog_id"], "BUG-TL")

    def test_list_events_filters_by_backlog_id_without_task_id(self):
        from agent.governance import task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-a",
            backlog_id="BUG-A",
            event_type="task.started",
            actor="worker-a",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-b",
            backlog_id="BUG-A",
            event_type="task.completed",
            actor="worker-b",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-c",
            backlog_id="BUG-B",
            event_type="task.completed",
            actor="worker-c",
        )

        events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-A")

        self.assertEqual([event["task_id"] for event in events], ["task-a", "task-b"])
        self.assertEqual({event["backlog_id"] for event in events}, {"BUG-A"})

    def test_mf_process_timeline_records_queryable_test_scenario_decision(self):
        from agent.governance import task_timeline

        verification = task_timeline.mf_test_scenario_verification({
            "test_scenario_policy": "new_scenario_required",
            "test_scenario_spec": {
                "id": "scn-mf-timeline",
                "name": "MF timeline schema scenario",
                "steps": [
                    "record the observer scenario decision",
                    "record the implementation/gate result against the same scenario",
                ],
                "expected": [
                    "timeline rows are queryable by scenario and correlation",
                    "gate evidence keeps a parent pointer to the scenario decision",
                ],
            },
            "verification_notes": ["scenario was designed before implementation"],
        })
        self.assertTrue(verification["passed"], verification)

        scenario_event = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF",
            mf_id="MF-20260523",
            task_id="task-mf",
            attempt_num=1,
            event_type="mf.test_scenario.decision",
            phase="plan",
            event_kind="scenario_spec",
            scenario_id="scn-mf-timeline",
            correlation_id="corr-mf-1",
            severity="info",
            decision="required",
            actor="observer",
            status="accepted",
            payload={
                "test_scenario_policy": "new_scenario_required",
                "test_scenario_spec": {
                    "id": "scn-mf-timeline",
                    "steps": ["record scenario", "record gate result"],
                    "expected": ["rows can be filtered"],
                },
            },
            verification=verification,
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF",
            mf_id="MF-20260523",
            task_id="task-mf",
            attempt_num=1,
            event_type="gate.mf_scenario.verified",
            phase="gate",
            event_kind="gate_result",
            scenario_id="scn-mf-timeline",
            parent_event_id=scenario_event["id"],
            correlation_id="corr-mf-1",
            severity="info",
            decision="approved",
            actor="gate",
            status="passed",
            verification={"passed": True, "checks": {"scenario_executed": True}},
        )
        self.conn.commit()

        events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id="BUG-MF",
            scenario_id="scn-mf-timeline",
            correlation_id="corr-mf-1",
        )

        self.assertEqual([event["event_kind"] for event in events], ["scenario_spec", "gate_result"])
        self.assertEqual(events[0]["phase"], "plan")
        self.assertEqual(events[0]["decision"], "required")
        self.assertEqual(events[0]["schema_version"], 2)
        self.assertEqual(events[1]["parent_event_id"], scenario_event["id"])

        gate_events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id="BUG-MF",
            scenario_id="scn-mf-timeline",
            event_kind="gate_result",
        )
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0]["event_type"], "gate.mf_scenario.verified")

    def test_mf_test_scenario_policy_verification(self):
        from agent.governance import task_timeline

        cases = [
            (
                "none with note",
                {"test_scenario_policy": "none", "verification_notes": ["copy-only README wording"]},
                True,
            ),
            (
                "none without note",
                {"test_scenario_policy": "none"},
                False,
            ),
            (
                "reuse existing with test command",
                {
                    "test_scenario_policy": "reuse_existing",
                    "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
                },
                True,
            ),
            (
                "reuse existing without evidence",
                {"test_scenario_policy": "reuse_existing"},
                False,
            ),
            (
                "new scenario missing spec",
                {"test_scenario_policy": "new_scenario_required", "verification_notes": ["high-risk path"]},
                False,
            ),
            (
                "new scenario with spec",
                {
                    "test_scenario_policy": "new_scenario_required",
                    "test_scenario_spec": {
                        "id": "scn-new",
                        "steps": ["seed fixture", "run MF command"],
                        "expected": ["gate sees scenario evidence"],
                    },
                },
                True,
            ),
        ]
        for label, payload, expected in cases:
            with self.subTest(label=label):
                result = task_timeline.mf_test_scenario_verification(payload)
                self.assertEqual(result["passed"], expected, result)

    def test_mf_close_gate_requires_observer_execution_evidence(self):
        from agent.governance import task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.implementation.completed",
            phase="implement",
            event_kind="implementation",
            actor="observer",
            status="passed",
            payload={"changed_files": ["agent/governance/server.py"]},
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.verification.completed",
            phase="verify",
            event_kind="verification",
            actor="observer",
            status="passed",
            verification={"tests_run": ["pytest -q agent/tests/test_task_timeline.py"]},
        )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-MF-GATE")
        blocked = task_timeline.mf_close_gate_verification(events)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], ["close_ready"])

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.close_ready.accepted",
            phase="close",
            event_kind="close_ready",
            actor="observer",
            status="accepted",
            verification={"graph_reconciled": True, "preflight_ok": True},
        )
        self.conn.commit()

        ready_events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-MF-GATE")
        ready = task_timeline.mf_close_gate_verification(ready_events)

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["present_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_db_migration_from_v41_adds_timeline_v2_columns_and_indexes(self):
        from agent.governance import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', '41');
            CREATE TABLE task_timeline_events (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id           TEXT NOT NULL,
                backlog_id           TEXT NOT NULL DEFAULT '',
                mf_id                TEXT NOT NULL DEFAULT '',
                task_id              TEXT NOT NULL DEFAULT '',
                attempt_num          INTEGER NOT NULL DEFAULT 0,
                event_type           TEXT NOT NULL,
                actor                TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT '',
                payload_json         TEXT NOT NULL DEFAULT '{}',
                verification_json    TEXT NOT NULL DEFAULT '{}',
                artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
                trace_id             TEXT NOT NULL DEFAULT '',
                commit_sha           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL
            );
        """)

        db._ensure_schema(conn)

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(task_timeline_events)").fetchall()
        }
        self.assertIn("phase", columns)
        self.assertIn("event_kind", columns)
        self.assertIn("scenario_id", columns)
        self.assertIn("correlation_id", columns)
        self.assertIn("schema_version", columns)
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(task_timeline_events)").fetchall()
        }
        self.assertIn("idx_task_timeline_scenario", indexes)
        self.assertIn("idx_task_timeline_kind", indexes)
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, "42")
        conn.close()

    def test_task_timeline_list_handler_filters_by_backlog_id_query(self):
        from agent.governance import server, task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-a",
            backlog_id="BUG-A",
            event_type="task.started",
            actor="worker-a",
            trace_id="trace-a",
            phase="implement",
            event_kind="observation",
            scenario_id="scn-handler",
            correlation_id="corr-handler",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-b",
            backlog_id="BUG-A",
            event_type="task.completed",
            actor="worker-b",
            trace_id="trace-b",
            phase="gate",
            event_kind="gate_result",
            scenario_id="scn-handler",
            correlation_id="corr-handler",
            decision="approved",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-c",
            backlog_id="BUG-B",
            event_type="task.completed",
            actor="worker-c",
            trace_id="trace-c",
        )
        self.conn.commit()

        result = server.handle_task_timeline_list(_ctx({"backlog_id": "BUG-A"}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["project_id"], "proj")
        self.assertEqual(result["task_id"], "")
        self.assertEqual(result["backlog_id"], "BUG-A")
        self.assertEqual(result["trace_id"], "")
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [event["task_id"] for event in result["events"]],
            ["task-a", "task-b"],
        )

        filtered = server.handle_task_timeline_list(
            _ctx({
                "backlog_id": "BUG-A",
                "task_id": "task-b",
                "trace_id": "trace-b",
                "phase": "gate",
                "event_kind": "gate_result",
                "scenario_id": "scn-handler",
                "correlation_id": "corr-handler",
                "decision": "approved",
                "limit": ["5"],
            })
        )
        self.assertEqual(filtered["task_id"], "task-b")
        self.assertEqual(filtered["trace_id"], "trace-b")
        self.assertEqual(filtered["phase"], "gate")
        self.assertEqual(filtered["event_kind"], "gate_result")
        self.assertEqual(filtered["scenario_id"], "scn-handler")
        self.assertEqual(filtered["correlation_id"], "corr-handler")
        self.assertEqual(filtered["decision"], "approved")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["events"][0]["task_id"], "task-b")

    def test_backlog_timeline_gate_precheck_matches_close_gate_evidence(self):
        from agent.governance import server, task_timeline

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-PRECHECK"},
                body={
                    "title": "MF timeline precheck",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                },
                method="POST",
            )
        )

        for kind in ("implementation", "verification"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-PRECHECK",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        self.conn.commit()

        blocked = server.handle_backlog_timeline_gate(
            _ctx({"include_events": "true"}, path_params={"bug_id": "BUG-MF-PRECHECK"})
        )

        self.assertTrue(blocked["ok"])
        self.assertTrue(blocked["applicable"])
        self.assertFalse(blocked["can_close"])
        self.assertEqual(blocked["timeline_gate"]["missing_event_kinds"], ["close_ready"])
        self.assertEqual(blocked["event_count"], 2)
        self.assertEqual(len(blocked["events"]), 2)

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-PRECHECK",
            event_type="mf.close_ready",
            phase="close",
            event_kind="close_ready",
            status="accepted",
        )
        self.conn.commit()

        ready = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-PRECHECK"})
        )
        self.assertTrue(ready["can_close"])
        self.assertTrue(ready["timeline_gate"]["passed"])
        self.assertEqual(
            ready["timeline_gate"]["present_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )


if __name__ == "__main__":
    unittest.main()
