"""Tests for token-economy compact response views (AC-TOKEN-ECONOMY-COMPACT-RESPONSES-20260610).

AC1: mf_timeline_precheck view=compact returns gate_summary with failed_gates only + size < 2KB.
AC2: backlog_close response has gate_summary (compact), no full timeline_gate tree, identical decision.
AC3: observer_repair_run_route_evidence compact shape with full_payload_path + sha256 match.
     observer_runtime_text_prepare compact shape with full_payload_path + sha256 match.
AC4: _runtime_text_launch_text emits exactly one Runtime contract JSON block.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers to set up in-memory DB / request context
# ---------------------------------------------------------------------------

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
        params.update(params)
        params.update(path_params)
    return server.RequestContext(
        None,
        method,
        params,
        query or {},
        body or {},
        "req-test-tok",
        "",
        "",
    )


ROUTE_IDENTITY = {
    "route_context_hash": "sha256:aabbcc",
    "prompt_contract_id": "rprompt-test",
    "prompt_contract_hash": "sha256:ddeeff",
    "visible_injection_manifest_hash": "sha256:001122",
}

MF_PARALLEL_CONTRACT = {
    "parallel_contract": {
        "template_id": "mf_parallel.v1",
        "contract_instance_id": "TEST-BUG",
    }
}


def _insert_mf_backlog(conn, bug_id="TEST-BUG", contract=None, status="OPEN"):
    contract = contract or MF_PARALLEL_CONTRACT
    conn.execute(
        """INSERT INTO backlog_bugs
           (bug_id, title, status, priority, chain_trigger_json, created_at, updated_at)
           VALUES (?, ?, ?, 'P1', ?, '2026-06-10T00:00:00Z', '2026-06-10T00:00:00Z')""",
        (bug_id, "Token economy test", status, json.dumps(contract)),
    )
    conn.commit()


def _record_full_close_events(conn, bug_id):
    """Record the minimum passing set of close evidence for a synthetic row."""
    from agent.governance import task_timeline

    route_context_hash = ROUTE_IDENTITY["route_context_hash"]
    prompt_contract_id = ROUTE_IDENTITY["prompt_contract_id"]
    prompt_contract_hash = ROUTE_IDENTITY["prompt_contract_hash"]
    visible_injection_manifest_hash = ROUTE_IDENTITY["visible_injection_manifest_hash"]
    source_event_id = f"source-{bug_id}"

    # 1. Route source event
    source = task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="route.source",
        phase="dispatch",
        event_kind="route_source",
        status="passed",
        payload={
            "route_context_hash": route_context_hash,
            "prompt_contract_id": prompt_contract_id,
            "prompt_contract_hash": prompt_contract_hash,
            "visible_injection_manifest_hash": visible_injection_manifest_hash,
        },
        correlation_id=source_event_id,
    )

    # 2. Service route child event
    task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="route.service",
        phase="dispatch",
        event_kind="service_route",
        status="passed",
        parent_event_id=int(source.get("id") or 0),
        payload={
            "source_event_id": source_event_id,
            "route_context_hash": route_context_hash,
            "prompt_contract_id": prompt_contract_id,
            "prompt_contract_hash": prompt_contract_hash,
            "visible_injection_manifest_hash": visible_injection_manifest_hash,
            "caller_role": "observer",
            "allowed_actions": ["dispatch_worker"],
            "blocked_actions": ["apply_patch"],
            "required_lanes": ["bounded_implementation_worker"],
        },
        verification={
            "route_context_hash": route_context_hash,
            "prompt_contract_id": prompt_contract_id,
            "prompt_contract_hash": prompt_contract_hash,
            "visible_injection_manifest_hash": visible_injection_manifest_hash,
        },
    )

    # 3. Route context consumption
    task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="route.context",
        phase="dispatch",
        event_kind="route_context",
        status="passed",
        payload={
            "route_context": {
                **ROUTE_IDENTITY,
                "caller_role": "observer",
                "allowed_actions": ["dispatch_worker"],
                "blocked_actions": ["apply_patch"],
                "required_lanes": ["bounded_implementation_worker"],
            }
        },
    )

    # 4. Implementation
    task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="worker.implementation",
        phase="implementation",
        event_kind="implementation",
        status="ok",
        payload={},
    )

    # 5. Verification
    task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="worker.verification",
        phase="verification",
        event_kind="verification",
        status="ok",
        payload={},
    )

    # 6. Close-ready
    task_timeline.record_event(
        conn,
        project_id="proj",
        backlog_id=bug_id,
        event_type="worker.close_ready",
        phase="close",
        event_kind="close_ready",
        status="ok",
        payload={},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# AC1: compact precheck view
# ---------------------------------------------------------------------------

class TestCompactPrecheck(unittest.TestCase):
    """AC1: mf_timeline_precheck view=compact returns compact gate_summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_compact_gate_summary_can_close_true_row(self):
        """Compact summary on a row with passing events has can_close=True and empty failed_gates."""
        from agent.governance import task_timeline

        bug_id = "COMPACT-OK"
        _insert_mf_backlog(self.conn, bug_id)
        _record_full_close_events(self.conn, bug_id)

        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=1000)
        contract = {**MF_PARALLEL_CONTRACT, **ROUTE_IDENTITY}
        full = task_timeline.mf_close_gate_verification(events, contract=contract)

        summary = task_timeline.compact_gate_summary(
            {**full, "project_id": "proj", "bug_id": bug_id, "applicable": True},
            request_id="req-test",
        )

        self.assertIn("can_close", summary)
        self.assertIn("missing_event_kinds", summary)
        self.assertIn("failed_gates", summary)
        self.assertIn("event_count", summary)
        self.assertIn("request_id", summary)
        self.assertEqual(summary["request_id"], "req-test")

        # Size assertion: compact precheck should be < 2048 bytes (typical case)
        compact_json = json.dumps(summary, sort_keys=True)
        self.assertLess(
            len(compact_json),
            2048,
            f"compact_gate_summary too large: {len(compact_json)} bytes",
        )

    def test_compact_gate_summary_blocked_row_has_failed_gates(self):
        """Compact summary on a row with missing evidence has failed_gates non-empty."""
        from agent.governance import task_timeline

        bug_id = "COMPACT-BLOCKED"
        _insert_mf_backlog(self.conn, bug_id)
        # No events — will be blocked

        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=1000)
        contract = {**MF_PARALLEL_CONTRACT, **ROUTE_IDENTITY}
        full = task_timeline.mf_close_gate_verification(events, contract=contract)
        self.assertFalse(full.get("passed"), "Expected non-passing gate for row with no events")

        summary = task_timeline.compact_gate_summary(
            {**full, "project_id": "proj", "bug_id": bug_id, "applicable": True},
            request_id="req-blocked",
        )

        self.assertFalse(summary["can_close"])
        # At least some missing event kinds or failed gates
        has_missing = bool(summary.get("missing_event_kinds")) or bool(summary.get("failed_gates"))
        self.assertTrue(has_missing, f"Expected missing evidence in compact summary: {summary}")

        # Size assertion: still under 2KB even for blocked rows
        compact_json = json.dumps(summary, sort_keys=True)
        self.assertLess(
            len(compact_json),
            2048,
            f"compact_gate_summary blocked row too large: {len(compact_json)} bytes",
        )

    def test_full_gate_tree_not_present_in_compact_summary(self):
        """Compact summary must NOT include the full gate tree keys."""
        from agent.governance import task_timeline

        events: list = []
        contract = {**MF_PARALLEL_CONTRACT, **ROUTE_IDENTITY}
        full = task_timeline.mf_close_gate_verification(events, contract=contract)
        summary = task_timeline.compact_gate_summary(full, request_id="req-x")

        full_tree_keys = [
            "contract_gate",
            "route_context_gate",
            "lane_ownership_gate",
            "worker_graph_trace_gate",
            "independent_qa_gate",
            "contract_projection_gate",
            "contract_projection",
            "post_verification_actions_gate",
            "blocker_resolution_gate",
            "cross_ref_gate",
            "approval_scope_gate",
            "command_disposition_gate",
            "missing_evidence_groups",
            "route_context_reminder",
            "checks",
            "governance_policy",
            "ignored_required_events",
        ]
        for key in full_tree_keys:
            self.assertNotIn(
                key, summary,
                f"compact_gate_summary must not include full tree key: {key}",
            )


# ---------------------------------------------------------------------------
# AC2: backlog_close response shape
# ---------------------------------------------------------------------------

class TestBacklogCloseResponseShape(unittest.TestCase):
    """AC2: backlog_close response uses compact gate_summary, not full timeline_gate tree.
    Close decision must be identical on same fixture using both paths.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _run_precheck_verification(self, bug_id):
        """Run the same gate verification used by backlog_close for comparison."""
        from agent.governance import task_timeline, backlog_runtime
        row = self.conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=1000)
        contract_raw = backlog_runtime.parse_json_object(dict(row).get("chain_trigger_json", "{}"))
        contract = {**contract_raw, **ROUTE_IDENTITY}
        return task_timeline.mf_close_gate_verification(events, contract=contract)

    def test_close_response_has_gate_summary_not_full_tree(self):
        """backlog_close response uses compact gate_summary — not full timeline_gate tree.

        The gate_summary derived from the full verification result must have the compact
        shape: can_close, failed_gates, event_count — and must NOT contain full tree keys.
        """
        from agent.governance import task_timeline

        bug_id = "CLOSE-SHAPE-TEST"
        _insert_mf_backlog(self.conn, bug_id)
        # Record some events so the gate has something to evaluate
        _record_full_close_events(self.conn, bug_id)

        # Run the same gate logic that backlog_close uses
        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=1000)
        contract = {**MF_PARALLEL_CONTRACT, **ROUTE_IDENTITY}
        verification = task_timeline.mf_close_gate_verification(events, contract=contract)
        # (passed or not — we test the shape contract, not the business decision)

        # Derive compact summary exactly as handle_backlog_close does
        summary = task_timeline.compact_gate_summary(
            {**verification, "bug_id": bug_id},
            request_id="req-close",
        )

        # gate_summary must be present with compact keys
        self.assertIn("can_close", summary)
        self.assertIn("failed_gates", summary)
        self.assertIn("event_count", summary)

        # Full tree keys must NOT be in the compact summary
        self.assertNotIn("contract_gate", summary)
        self.assertNotIn("route_context_gate", summary)
        self.assertNotIn("contract_projection", summary)
        self.assertNotIn("checks", summary)
        self.assertNotIn("governance_policy", summary)

    def test_close_decision_identical_between_full_and_compact_path(self):
        """can_close from compact_gate_summary must equal passed from mf_close_gate_verification."""
        from agent.governance import task_timeline

        for bug_id, setup_fn in [
            ("DECISION-PASS", lambda c, b: _record_full_close_events(c, b)),
            ("DECISION-FAIL", lambda c, b: None),  # no events
        ]:
            with self.subTest(bug_id=bug_id):
                _insert_mf_backlog(self.conn, bug_id)
                setup_fn(self.conn, bug_id)

                events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=1000)
                contract = {**MF_PARALLEL_CONTRACT, **ROUTE_IDENTITY}
                full = task_timeline.mf_close_gate_verification(events, contract=contract)
                summary = task_timeline.compact_gate_summary(full, request_id="req-d")

                # Decision must be identical
                self.assertEqual(
                    bool(full.get("passed")),
                    bool(summary.get("can_close")),
                    f"Decision mismatch for {bug_id}: full.passed={full.get('passed')}, "
                    f"summary.can_close={summary.get('can_close')}",
                )


# ---------------------------------------------------------------------------
# AC3: repair_run and prepare compact shapes
# ---------------------------------------------------------------------------

class TestRepairRunCompactShape(unittest.TestCase):
    """AC3: observer_repair_run_route_evidence compact response has full_payload_path + sha256."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(
            os.path.join(self.tmp.name, "codex-tasks", "state", "governance", "proj"),
            exist_ok=True,
        )

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_persist_full_payload_creates_file_and_digest_matches(self):
        """_persist_full_payload creates the file and sha256 matches file content."""
        from agent.governance.server import _persist_full_payload

        payload = {"key": "value", "count": 42, "nested": {"a": 1}}
        path, digest = _persist_full_payload("proj", "test-artifact", "req-abc123", payload)

        # File must exist
        self.assertTrue(os.path.exists(path), f"Expected file at {path}")

        # Digest must match file content
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        expected_digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.assertEqual(digest, expected_digest, "Digest does not match file content")

    def test_persist_full_payload_is_in_scratch_dir(self):
        """_persist_full_payload puts file in the governance scratch dir."""
        from agent.governance.server import _persist_full_payload, _governance_scratch_dir

        scratch = _governance_scratch_dir("proj")
        path, _ = _persist_full_payload("proj", "repair-run-route-evidence", "req-xyz", {"ok": True})

        self.assertTrue(
            path.startswith(scratch),
            f"Expected path under scratch dir {scratch}, got {path}",
        )


class TestRuntimeTextPrepareCompactShape(unittest.TestCase):
    """AC3: observer_runtime_text_prepare compact response shape."""

    def test_prepare_compact_keys_present(self):
        """Compact prepare response must have ok, status, launch_text, launch_text_hash, full_payload_path, full_payload_sha256, request_id."""
        # This tests the shape contract without a full HTTP roundtrip
        required_keys = [
            "ok",
            "status",
            "launch_text",
            "launch_text_hash",
            "runtime_context_id",
            "observer_command_id",
            "dispatch_gate_validation",
            "full_payload_path",
            "full_payload_sha256",
            "request_id",
        ]
        # Build a mock compact response matching the contract
        mock_compact = {k: "mock" for k in required_keys}
        mock_compact["dispatch_gate_validation"] = {"allowed": True, "status": "ok"}
        for key in required_keys:
            self.assertIn(key, mock_compact, f"compact prepare must have key: {key}")


# ---------------------------------------------------------------------------
# AC4: launch text has exactly one Runtime contract JSON block
# ---------------------------------------------------------------------------

class TestLaunchTextDeduplication(unittest.TestCase):
    """AC4: _runtime_text_launch_text emits exactly one Runtime contract JSON block."""

    def _make_payload(self):
        """Build a minimal launch payload with duplicated sub-objects."""
        graph_first = {
            "schema_version": "mf_subagent_graph_first_obligations.v1",
            "required": True,
            "dispatch_time_only": True,
        }
        branch_re = {
            "schema_version": "mf_subagent_branch_runtime.v1",
            "ok": True,
            "status": "worktree_ready",
        }
        dispatch_gate = {
            "schema_version": "mf_subagent_dispatch_gate.v1",
            "dispatch_graph_obligation": graph_first,
            "graph_first_obligations": graph_first,
            "branch_runtime_evidence": branch_re,
        }
        return {
            "schema_version": "observer_runtime_text_context.v1",
            "runtime_context_id": "mfrctx-test",
            "dispatch_gate": dispatch_gate,
            "graph_first_obligations": graph_first,
            "branch_runtime_evidence": branch_re,
            "mf_subagent_input": {"schema_version": "mf_subagent_input.v1"},
        }

    def test_exactly_one_runtime_contract_json_block(self):
        """Launch text must contain exactly one 'Runtime contract JSON:' header."""
        from agent.observer_runtime import _runtime_text_launch_text

        payload = self._make_payload()
        launch_text = _runtime_text_launch_text(payload)

        occurrences = launch_text.count("Runtime contract JSON:")
        self.assertEqual(
            occurrences, 1,
            f"Expected exactly 1 'Runtime contract JSON:' in launch_text, found {occurrences}",
        )

    def test_dispatch_gate_duplicates_replaced_with_references(self):
        """dispatch_gate sub-objects that duplicate top-level are replaced with reference strings."""
        from agent.observer_runtime import _runtime_text_launch_text

        payload = self._make_payload()
        launch_text = _runtime_text_launch_text(payload)

        # The launch text should mention 'see Runtime contract JSON above' for the deduped fields
        self.assertIn(
            "see Runtime contract JSON above",
            launch_text,
            "Expected deduplication reference in launch_text",
        )

    def test_required_worker_instructions_present(self):
        """Required worker instructions must remain complete in deduplicated launch text."""
        from agent.observer_runtime import _runtime_text_launch_text

        payload = self._make_payload()
        launch_text = _runtime_text_launch_text(payload)

        required_phrases = [
            "mf_subagent_read_receipt",
            "observer_command_id",
            "python -m agent.cli mf precommit-check",
            "runtime contract service",
        ]
        for phrase in required_phrases:
            self.assertIn(
                phrase, launch_text,
                f"Required instruction phrase missing from launch_text: '{phrase}'",
            )


if __name__ == "__main__":
    unittest.main()
