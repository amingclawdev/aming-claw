"""Tests for Coordinator Decision Paths.

Covers the 9 decision types a coordinator can produce, plus:
  - Gateway pre-filter (greeting/query/dangerous skip coordinator)
  - PM analysis gating (_needs_pm_analysis)
  - Conflict rules integration (duplicate/conflict/new)
  - 4-layer validation (schema/policy/graph/precondition)
  - Memory search instrumentation
  - Observer mode task hold interaction
  - Auto-chain trigger for PM-created tasks vs non-chain types

These tests run against real governance DB (in-memory) and real
conflict_rules / task_registry logic — no mocking of core paths.
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch
from pathlib import Path

# Setup path
agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)
os.environ.setdefault("MEMORY_BACKEND", "local")


def _fresh_conn(project_id="test-proj"):
    """Get a fresh in-memory-style governance DB connection."""
    from governance.db import get_connection
    conn = get_connection(project_id)
    return conn


class TestGatewayPreFilter(unittest.TestCase):
    """Gateway classify_message determines what reaches coordinator."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "telegram_gateway"))

    def test_greeting_skips_coordinator(self):
        from gateway import classify_message
        for msg in ["hello", "hi there", "thanks"]:
            result = classify_message(msg)
            self.assertIn(result, ("greeting", "chat"),
                          f"Greeting '{msg}' should not reach coordinator as task")

    def test_query_skips_coordinator(self):
        from gateway import classify_message
        self.assertEqual(classify_message("status?"), "query")
        self.assertEqual(classify_message("show nodes"), "query")

    def test_task_reaches_coordinator(self):
        from gateway import classify_message
        result = classify_message("add a login page to the app")
        self.assertIn(result, ("task", "chat"),
                      "Task request should reach coordinator")


class TestPMAnalysisGating(unittest.TestCase):
    """_needs_pm_analysis decides if PM runs before coordinator."""

    def _needs_pm(self, text):
        from task_orchestrator import TaskOrchestrator
        return TaskOrchestrator._needs_pm_analysis(None, text)

    def test_query_no_pm(self):
        self.assertFalse(self._needs_pm("status"))
        self.assertFalse(self._needs_pm("list all nodes"))
        self.assertFalse(self._needs_pm("show pending tasks"))

    def test_feature_request_needs_pm(self):
        self.assertTrue(self._needs_pm("add a new login page"))
        self.assertTrue(self._needs_pm("implement dark mode"))
        self.assertTrue(self._needs_pm("refactor the auth module"))

    def test_fix_request_needs_pm(self):
        self.assertTrue(self._needs_pm("fix the timeout bug"))
        self.assertTrue(self._needs_pm("update the error handling"))

    def test_ambiguous_defaults_to_pm(self):
        # Per coordinator-rules.md: non-query messages always go to PM
        self.assertTrue(self._needs_pm("hello world"))
        self.assertTrue(self._needs_pm("what is this?"))
        self.assertTrue(self._needs_pm("change the timeout mechanism"))


class TestConflictRulesDecisions(unittest.TestCase):
    """Conflict rules produce: new, duplicate, conflict, queue, retry."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_new_task_no_conflicts(self):
        from governance.conflict_rules import check_conflicts
        conn = _fresh_conn("conflict-test-1")
        result = check_conflicts(
            conn, "conflict-test-1",
            target_files=["agent/foo.py"],
            operation_type="add",
            intent_hash="abc123",
            prompt="Add a new feature",
        )
        self.assertEqual(result["decision"], "new")
        conn.close()

    def test_duplicate_detection(self):
        from governance.conflict_rules import check_conflicts, compute_intent_hash
        from governance.task_registry import create_task
        conn = _fresh_conn("conflict-test-2")

        prompt = "Add dark mode toggle"
        intent_hash = compute_intent_hash(prompt)

        # Create first task
        create_task(conn, "conflict-test-2", prompt=prompt, task_type="pm",
                    metadata={"operation_type": "add", "intent_hash": intent_hash})
        conn.commit()

        # Check for duplicate
        result = check_conflicts(
            conn, "conflict-test-2",
            target_files=[],
            operation_type="add",
            intent_hash=intent_hash,
            prompt=prompt,
        )
        self.assertEqual(result["decision"], "duplicate")
        conn.close()

    def test_opposite_operation_conflict(self):
        from governance.conflict_rules import check_conflicts
        from governance.task_registry import create_task
        conn = _fresh_conn("conflict-test-3")

        # Create an active "add" task on auth.py
        create_task(conn, "conflict-test-3", prompt="Add auth feature",
                    task_type="dev",
                    metadata={"operation_type": "add",
                              "target_files": ["agent/auth.py"],
                              "intent_hash": "x1"})
        conn.commit()

        # Try to delete the same file
        result = check_conflicts(
            conn, "conflict-test-3",
            target_files=["agent/auth.py"],
            operation_type="delete",
            intent_hash="x2",
            prompt="Delete auth module",
        )
        self.assertEqual(result["decision"], "conflict")
        self.assertIn("auth.py", result["reason"])
        conn.close()


class TestCoordinatorActionTypes(unittest.TestCase):
    """Validate that all 9 action types are recognized by the validator."""

    def test_coordinator_allowed_actions(self):
        from role_permissions import ROLE_PERMISSIONS
        allowed = ROLE_PERMISSIONS["coordinator"]["allowed"]
        # Coordinator has NO tools — only reply_only and create_pm_task
        expected = {"create_pm_task", "reply_only"}
        self.assertEqual(allowed, expected)

    def test_coordinator_denied_actions(self):
        from role_permissions import ROLE_PERMISSIONS
        denied = ROLE_PERMISSIONS["coordinator"]["denied"]
        # Everything except reply_only and create_pm_task is denied
        for action in ["modify_code", "run_tests", "verify_update",
                        "release_gate", "run_command", "execute_script",
                        "create_dev_task", "create_test_task", "create_qa_task",
                        "query_governance", "update_context", "archive_memory",
                        "propose_node", "propose_node_update"]:
            self.assertIn(action, denied,
                          f"Coordinator should be denied {action}")


class TestValidationLayers(unittest.TestCase):
    """4-layer validation: schema, policy, graph, precondition."""

    def _validate(self, role, ai_output, project_id="test-val"):
        from decision_validator import DecisionValidator
        v = DecisionValidator()
        return v.validate(role, ai_output, project_id)

    def test_reply_only_passes_all_layers(self):
        result = self._validate("coordinator", {
            "reply": "Hello!",
            "actions": [{"type": "reply_only"}],
        })
        self.assertEqual(len(result.approved_actions), 1)
        self.assertEqual(len(result.rejected_actions), 0)

    def test_create_pm_task_passes(self):
        result = self._validate("coordinator", {
            "reply": "Creating PM task",
            "actions": [{"type": "create_pm_task", "prompt": "Design login page"}],
        })
        self.assertEqual(len(result.approved_actions), 1)
        self.assertEqual(result.approved_actions[0]["type"], "create_pm_task")

    def test_policy_rejects_modify_code_for_coordinator(self):
        result = self._validate("coordinator", {
            "reply": "Let me fix that",
            "actions": [{"type": "modify_code", "file": "agent/foo.py"}],
        })
        self.assertEqual(len(result.rejected_actions), 1)
        self.assertIn("modify_code", str(result.rejected_actions[0]))

    def test_multiple_actions_partial_approval(self):
        # Only create_pm_task and reply_only are allowed; everything else denied
        result = self._validate("coordinator", {
            "reply": "Processing",
            "actions": [
                {"type": "create_pm_task", "prompt": "Design fix"},
                {"type": "create_dev_task", "prompt": "Fix bug"},  # denied
                {"type": "modify_code", "file": "x.py"},  # denied
                {"type": "reply_only"},  # allowed
            ],
        })
        approved_types = [a["type"] for a in result.approved_actions]
        self.assertIn("create_pm_task", approved_types)
        self.assertIn("reply_only", approved_types)
        self.assertNotIn("create_dev_task", approved_types)
        self.assertNotIn("modify_code", [str(a) for a in approved_types])
        self.assertTrue(len(result.rejected_actions) >= 2)


class TestObserverModeInteraction(unittest.TestCase):
    """Observer mode affects task creation status."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _ensure_project_version(self, conn, project_id):
        """Ensure project_version row exists for observer_mode tests."""
        conn.execute(
            "INSERT OR IGNORE INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES (?, '', datetime('now'), 'test')",
            (project_id,),
        )
        conn.commit()

    def test_observer_mode_off_creates_queued(self):
        from governance.task_registry import create_task, set_observer_mode
        conn = _fresh_conn("obs-test-1")
        self._ensure_project_version(conn, "obs-test-1")
        set_observer_mode(conn, "obs-test-1", False)
        conn.commit()
        result = create_task(conn, "obs-test-1", prompt="test", task_type="task")
        self.assertEqual(result["status"], "queued")
        conn.close()

    def test_observer_mode_on_creates_hold(self):
        from governance.task_registry import create_task, set_observer_mode
        conn = _fresh_conn("obs-test-2")
        self._ensure_project_version(conn, "obs-test-2")
        set_observer_mode(conn, "obs-test-2", True)
        conn.commit()
        result = create_task(conn, "obs-test-2", prompt="test", task_type="task")
        self.assertEqual(result["status"], "observer_hold")
        conn.close()

    def test_hold_release_cycle(self):
        from governance.task_registry import create_task, hold_task, release_task
        conn = _fresh_conn("obs-test-3")
        task = create_task(conn, "obs-test-3", prompt="test", task_type="pm")
        conn.commit()
        tid = task["task_id"]

        # queued → observer_hold
        hold_task(conn, tid)
        conn.commit()
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(row["execution_status"], "observer_hold")

        # observer_hold → queued
        release_task(conn, tid)
        conn.commit()
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(row["execution_status"], "queued")
        conn.close()

    def test_cancel_task(self):
        from governance.task_registry import create_task, cancel_task
        conn = _fresh_conn("cancel-test-1")
        task = create_task(conn, "cancel-test-1", prompt="test cancel", task_type="pm")
        conn.commit()
        result = cancel_task(conn, task["task_id"])
        conn.commit()
        self.assertEqual(result["status"], "cancelled")
        row = conn.execute("SELECT status, execution_status FROM tasks WHERE task_id=?",
                           (task["task_id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["execution_status"], "cancelled")
        conn.close()

    def test_cancelled_not_in_claim(self):
        from governance.task_registry import create_task, cancel_task, claim_task
        conn = _fresh_conn("cancel-test-2")
        task = create_task(conn, "cancel-test-2", prompt="test", task_type="pm")
        conn.commit()
        cancel_task(conn, task["task_id"])
        conn.commit()
        # Claim should find nothing (only cancelled task exists)
        claimed = claim_task(conn, "cancel-test-2", "worker-1")
        self.assertIsNone(claimed[0] if isinstance(claimed, tuple) else claimed)
        conn.close()

    def test_claim_skips_observer_hold(self):
        from governance.task_registry import create_task, claim_task, set_observer_mode
        conn = _fresh_conn("obs-test-4")
        self._ensure_project_version(conn, "obs-test-4")
        set_observer_mode(conn, "obs-test-4", True)
        conn.commit()

        create_task(conn, "obs-test-4", prompt="held task", task_type="pm")
        conn.commit()

        # claim_task should find nothing (only observer_hold tasks exist)
        result = claim_task(conn, "obs-test-4", "executor-1")
        self.assertIsNone(result[0] if isinstance(result, tuple) else result)
        conn.close()


class TestAutoChainTrigger(unittest.TestCase):
    """Only PM-type tasks trigger auto-chain; coordinator/dev_task do not."""

    def test_task_type_not_in_chain(self):
        from governance.auto_chain import CHAIN
        self.assertNotIn("task", CHAIN)
        self.assertNotIn("dev_task", CHAIN)
        self.assertNotIn("test_task", CHAIN)
        self.assertNotIn("qa_task", CHAIN)

    def test_pm_type_in_chain(self):
        from governance.auto_chain import CHAIN
        self.assertIn("pm", CHAIN)
        self.assertIn("dev", CHAIN)
        self.assertIn("test", CHAIN)
        self.assertIn("qa", CHAIN)
        self.assertIn("merge", CHAIN)

    def test_on_task_completed_skips_non_chain(self):
        """type='task' completion should not trigger chain."""
        from governance.auto_chain import on_task_completed
        result = on_task_completed(
            None, "test-proj", "task-xxx", "task", "succeeded", {}, {}
        )
        self.assertIsNone(result)

    def test_on_task_completed_skips_failed(self):
        """Failed tasks should not trigger chain."""
        from governance.auto_chain import on_task_completed
        result = on_task_completed(
            None, "test-proj", "task-xxx", "pm", "failed", {}, {}
        )
        self.assertIsNone(result)


class TestMemorySearchInstrumentation(unittest.TestCase):
    """Memory search returns results and logs them."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_search_empty_returns_list(self):
        from governance.memory_service import search_memories
        conn = _fresh_conn("mem-test-1")
        results = search_memories(conn, "mem-test-1", "observer mode")
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 0)
        conn.close()

    def test_write_then_search_finds_result(self):
        from governance.memory_service import search_memories, write_memory
        from governance.models import MemoryEntry
        conn = _fresh_conn("mem-test-2")
        entry = MemoryEntry(
            module_id="governance.observer",
            kind="decision",
            content="Observer mode holds tasks for manual review",
            created_by="test",
        )
        write_memory(conn, "mem-test-2", entry)
        conn.commit()

        results = search_memories(conn, "mem-test-2", "observer review")
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("observer", results[0]["content"].lower())
        conn.close()


class TestTaskLifecycleLogging(unittest.TestCase):
    """Task create/claim/complete have structured logging."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_claim_complete_lifecycle(self):
        """Full lifecycle: create → claim → complete."""
        from governance.task_registry import create_task, claim_task, complete_task
        conn = _fresh_conn("lifecycle-1")

        # Create
        task = create_task(conn, "lifecycle-1", prompt="test lifecycle",
                           task_type="pm", metadata={"target_files": ["a.py"]})
        conn.commit()
        self.assertEqual(task["status"], "queued")
        tid = task["task_id"]

        # Claim
        claimed, fence = claim_task(conn, "lifecycle-1", "worker-1")
        conn.commit()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], tid)

        # Complete
        result = complete_task(conn, tid, status="succeeded",
                               result={"prd_scope": "test"},
                               project_id="lifecycle-1")
        self.assertEqual(result["status"], "succeeded")
        conn.close()

    def test_complete_with_result_keys(self):
        """Verify result dict is stored correctly."""
        from governance.task_registry import create_task, claim_task, complete_task
        conn = _fresh_conn("lifecycle-2")

        task = create_task(conn, "lifecycle-2", prompt="test result",
                           task_type="task")
        conn.commit()
        tid = task["task_id"]

        claim_task(conn, "lifecycle-2", "observer")
        conn.commit()

        result_data = {
            "target_files": ["agent/foo.py"],
            "verification": "manual check",
            "acceptance_criteria": ["tests pass"],
        }
        result = complete_task(conn, tid, status="succeeded",
                               result=result_data, project_id="lifecycle-2")
        self.assertEqual(result["status"], "succeeded")

        # Verify stored in DB
        row = conn.execute("SELECT result_json FROM tasks WHERE task_id=?",
                           (tid,)).fetchone()
        stored = json.loads(row["result_json"])
        self.assertEqual(stored["target_files"], ["agent/foo.py"])
        conn.close()


class TestCoordinatorGate(unittest.TestCase):
    """Gate validation for coordinator JSON output format."""

    def _make_worker(self):
        """Create a minimal ExecutorWorker for testing gate validation."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        return worker

    def test_valid_reply_only_passes(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "reply": "Hello!",
            "actions": [{"type": "reply_only"}],
        })
        self.assertTrue(valid)
        self.assertEqual(err, "")

    def test_valid_create_pm_passes(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "reply": "Creating PM task",
            "actions": [{"type": "create_pm_task", "prompt": "A" * 60}],
        })
        self.assertTrue(valid)

    def test_missing_schema_version_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "reply": "Hello",
            "actions": [{"type": "reply_only"}],
        })
        self.assertFalse(valid)
        self.assertIn("schema_version", err)

    def test_empty_actions_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "reply": "Hello",
            "actions": [],
        })
        self.assertFalse(valid)
        self.assertIn("actions", err)

    def test_invalid_action_type_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "reply": "Creating dev task",
            "actions": [{"type": "create_dev_task", "prompt": "fix bug"}],
        })
        self.assertFalse(valid)
        self.assertIn("create_dev_task", err)

    def test_short_prompt_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "reply": "Creating PM",
            "actions": [{"type": "create_pm_task", "prompt": "too short"}],
        })
        self.assertFalse(valid)
        self.assertIn("too short", err)

    def test_missing_reply_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "reply": "",
            "actions": [{"type": "reply_only"}],
        })
        self.assertFalse(valid)
        self.assertIn("reply", err)

    def test_valid_query_memory_round1(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "actions": [{"type": "query_memory", "queries": ["executor timeout", "heartbeat"]}],
        }, round=1)
        self.assertTrue(valid, f"query_memory should pass in round 1: {err}")

    def test_query_memory_rejected_round2(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "actions": [{"type": "query_memory", "queries": ["test"]}],
        }, round=2)
        self.assertFalse(valid)
        self.assertIn("query_memory", err)

    def test_query_memory_empty_queries_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "actions": [{"type": "query_memory", "queries": []}],
        }, round=1)
        self.assertFalse(valid)

    def test_query_memory_too_many_queries_fails(self):
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "actions": [{"type": "query_memory", "queries": ["a", "b", "c", "d"]}],
        }, round=1)
        self.assertFalse(valid)

    def test_query_memory_no_reply_needed(self):
        """query_memory doesn't require reply field."""
        w = self._make_worker()
        valid, err = w._validate_coordinator_output({
            "schema_version": "v1",
            "actions": [{"type": "query_memory", "queries": ["test"]}],
        }, round=1)
        self.assertTrue(valid, f"query_memory should not require reply: {err}")


class TestBuildPromptCoordinator(unittest.TestCase):
    """Test executor._build_prompt coordinator branch assembles correct prompt."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        os.environ["GOVERNANCE_URL"] = "http://localhost:40000"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build(self, prompt, context=None):
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        worker.project_id = "test-build"
        worker.base_url = "http://localhost:40000"
        worker.workspace = self.tmpdir
        worker._lifecycle = None
        # Mock _fetch_memories and _api to avoid real API calls
        worker._fetch_memories = lambda q: [
            {"memory_id": "m1", "kind": "pitfall", "content": "test pitfall", "summary": "test pitfall"}
        ]
        worker._api = lambda method, path, body=None: {"tasks": [], "exists": False, "context": {}}
        return worker._build_prompt(prompt, "task", context or {})

    def test_contains_project_id(self):
        result = self._build("fix timeout bug")
        self.assertIn("test-build", result)

    def test_round1_no_memories(self):
        """Round 1 prompt should NOT contain memories (coordinator queries them via query_memory)."""
        result = self._build("fix timeout bug")
        self.assertNotIn("pitfall", result)
        self.assertNotIn("Relevant Memories", result)

    def test_round2_contains_memories(self):
        """Round 2 prompt should contain memory results from query_memory."""
        result = self._build("fix timeout bug", context={"_round2_memories": [
            {"kind": "pitfall", "content": "test pitfall memory", "summary": "test pitfall memory"}
        ]})
        self.assertIn("Memory Search Results", result)
        self.assertIn("test pitfall memory", result)

    def test_no_bash_api_instructions(self):
        """Coordinator prompt should NOT contain curl or API call instructions."""
        result = self._build("fix timeout bug")
        self.assertNotIn("curl", result.lower())
        self.assertNotIn("bash", result.lower())


class TestMemoryWriteNormalization(unittest.TestCase):
    """Test memory write translates Chinese content to English."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @unittest.mock.patch("governance.llm_utils._call_cli")
    def test_chinese_content_translated(self, mock_cli):
        mock_cli.return_value = "Gate blocked: PRD missing mandatory fields"
        from governance.memory_service import write_memory
        from governance.models import MemoryEntry
        conn = _fresh_conn("mem-norm-1")
        entry = MemoryEntry(
            module_id="test.module",
            kind="pitfall",
            content="Gate blocked: PRD缺少必填字段",
            created_by="test",
        )
        result = write_memory(conn, "mem-norm-1", entry)
        conn.commit()
        # Verify stored content is English
        row = conn.execute("SELECT content FROM memories WHERE memory_id=?",
                           (result["memory_id"],)).fetchone()
        self.assertNotIn("缺少", row["content"])
        conn.close()

    def test_english_content_unchanged(self):
        from governance.memory_service import write_memory
        from governance.models import MemoryEntry
        conn = _fresh_conn("mem-norm-2")
        entry = MemoryEntry(
            module_id="test.module",
            kind="pitfall",
            content="Already English content",
            created_by="test",
        )
        result = write_memory(conn, "mem-norm-2", entry)
        conn.commit()
        row = conn.execute("SELECT content FROM memories WHERE memory_id=?",
                           (result["memory_id"],)).fetchone()
        self.assertEqual(row["content"], "Already English content")
        conn.close()


class TestHandleCoordinatorResult(unittest.TestCase):
    """Test _handle_coordinator_result parses v1 JSON and executes actions."""

    def _make_worker(self):
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        worker.project_id = "test-handler"
        worker.base_url = "http://localhost:40000"
        worker._api_calls = []
        def mock_api(method, path, body=None):
            worker._api_calls.append((method, path, body))
            return {"task_id": "task-mock-001", "status": "observer_hold"}
        worker._api = mock_api
        worker._telegram_reply = lambda chat_id, text: None
        return worker

    def test_v1_create_pm_task(self):
        worker = self._make_worker()
        task = {"task_id": "task-test-1", "metadata": {}}
        result = {"raw_output": json.dumps({
            "schema_version": "v1",
            "reply": "Creating PM task",
            "actions": [{"type": "create_pm_task", "prompt": "A" * 60}],
            "context_update": {"current_focus": "test"},
        })}
        worker._handle_coordinator_result(task, result)
        # Verify PM task was created via API
        create_calls = [c for c in worker._api_calls if "create" in c[1]]
        self.assertGreaterEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0][2]["type"], "pm")

    def test_v1_reply_only(self):
        worker = self._make_worker()
        task = {"task_id": "task-test-2", "metadata": {}}
        result = {"raw_output": json.dumps({
            "schema_version": "v1",
            "reply": "Hello!",
            "actions": [{"type": "reply_only"}],
        })}
        worker._handle_coordinator_result(task, result)
        # No task creation API calls
        create_calls = [c for c in worker._api_calls if "create" in c[1]]
        self.assertEqual(len(create_calls), 0)

    def test_v1_rejected_action(self):
        worker = self._make_worker()
        task = {"task_id": "task-test-3", "metadata": {}}
        result = {"raw_output": json.dumps({
            "schema_version": "v1",
            "reply": "Let me fix that",
            "actions": [{"type": "create_dev_task", "prompt": "fix bug"}],
        })}
        worker._handle_coordinator_result(task, result)
        # create_dev_task should be rejected by gate — no create API calls
        create_calls = [c for c in worker._api_calls if "task" in c[1] and "create" in c[1]]
        self.assertEqual(len(create_calls), 0)

    def test_legacy_create_task(self):
        worker = self._make_worker()
        task = {"task_id": "task-test-4", "metadata": {}}
        result = {"raw_output": json.dumps({
            "action": "create_task",
            "type": "pm",
            "prompt": "B" * 60,
        })}
        worker._handle_coordinator_result(task, result)
        create_calls = [c for c in worker._api_calls if "create" in c[1]]
        self.assertGreaterEqual(len(create_calls), 1)

    def test_context_update_saved(self):
        worker = self._make_worker()
        task = {"task_id": "task-test-5", "metadata": {}}
        result = {"raw_output": json.dumps({
            "schema_version": "v1",
            "reply": "OK",
            "actions": [{"type": "reply_only"}],
            "context_update": {"current_focus": "auth", "last_decision": "reply_only"},
        })}
        worker._handle_coordinator_result(task, result)
        context_calls = [c for c in worker._api_calls if "context" in c[1]]
        self.assertGreaterEqual(len(context_calls), 1)


class TestPromptConsistency(unittest.TestCase):
    """Verify _build_prompt coordinator output contains key decision instructions."""

    def _get_role_prompt(self):
        from role_permissions import ROLE_PROMPTS
        return ROLE_PROMPTS.get("coordinator", "")

    def test_role_prompt_has_decision_rules(self):
        rp = self._get_role_prompt()
        self.assertIn("reply_only", rp)
        self.assertIn("create_pm_task", rp)

    def test_role_prompt_prohibits_dev_task(self):
        rp = self._get_role_prompt()
        self.assertIn("NEVER", rp)
        # Should prohibit direct dev/test/qa task creation
        self.assertTrue("dev/test/qa" in rp.lower() or "create_dev_task" in rp,
                        "Role prompt should mention prohibition of dev/test/qa tasks")

    def test_role_prompt_has_output_format(self):
        rp = self._get_role_prompt()
        self.assertIn("schema_version", rp)
        self.assertIn("v1", rp)
        self.assertIn("JSON", rp)

    def test_role_prompt_says_no_tools(self):
        """Coordinator role prompt should state no Bash/tool access."""
        rp = self._get_role_prompt()
        # Should explicitly say coordinator has no tools
        self.assertTrue("NO Bash" in rp or "no tools" in rp.lower() or "no bash" in rp.lower(),
                        "Role prompt should state coordinator has no Bash/tool access")

    def test_coordinator_allowed_only_two_actions(self):
        from role_permissions import ROLE_PERMISSIONS
        allowed = ROLE_PERMISSIONS["coordinator"]["allowed"]
        self.assertEqual(allowed, {"create_pm_task", "reply_only"})


class TestGatePostPM(unittest.TestCase):
    """PB1-PB5: Unit tests for _gate_post_pm explain-or-provide gate."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir
        os.environ["MEMORY_BACKEND"] = "local"
        from governance import memory_backend
        memory_backend._backend_instance = None

    def tearDown(self):
        from governance import memory_backend
        memory_backend._backend_instance = None
        os.environ.pop("SHARED_VOLUME_PATH", None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _gate(self, result, metadata=None):
        from governance.auto_chain import _gate_post_pm
        conn = _fresh_conn("gate-pm-test")
        return _gate_post_pm(conn, "gate-pm-test", result, metadata or {})

    def test_pb1_all_fields_pass(self):
        """PB1: All mandatory + soft fields provided → pass."""
        ok, msg = self._gate({
            "target_files": ["agent/executor_worker.py"],
            "verification": {"method": "test", "command": "pytest"},
            "acceptance_criteria": ["AC1", "AC2"],
            "test_files": ["agent/tests/test_foo.py"],
            "proposed_nodes": [{"title": "test node"}],
            "doc_impact": {"files": ["docs/x.md"], "changes": ["update"]},
        })
        self.assertTrue(ok, f"Should pass: {msg}")

    def test_pb2_missing_target_files(self):
        """PB2: Missing target_files → block."""
        ok, msg = self._gate({
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
        })
        self.assertFalse(ok)
        self.assertIn("target_files", msg)

    def test_pb3_missing_acceptance_criteria(self):
        """PB3: Missing acceptance_criteria → block."""
        ok, msg = self._gate({
            "target_files": ["agent/foo.py"],
            "verification": {"method": "test"},
        })
        self.assertFalse(ok)
        self.assertIn("acceptance_criteria", msg)

    def test_pb4_soft_field_empty_with_skip_reason(self):
        """PB4: Soft field empty + skip_reasons provided → pass."""
        ok, msg = self._gate({
            "target_files": ["agent/foo.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            "test_files": [],
            "proposed_nodes": [],
            "doc_impact": {},
            "skip_reasons": {
                "test_files": "no tests needed for config change",
                "proposed_nodes": "within scope of existing node",
                "doc_impact": "code-only, no docs",
            },
        })
        self.assertTrue(ok, f"Should pass with skip_reasons: {msg}")

    def test_pb5_soft_field_empty_no_reason(self):
        """PB5: Soft field empty + no skip_reasons → block."""
        ok, msg = self._gate({
            "target_files": ["agent/foo.py"],
            "verification": {"method": "test"},
            "acceptance_criteria": ["AC1"],
            # test_files, proposed_nodes, doc_impact all missing, no skip_reasons
        })
        self.assertFalse(ok)
        self.assertIn("skip_reasons", msg)


class TestParseOutput(unittest.TestCase):
    """Regression tests for executor output parsing."""

    def _make_worker(self):
        from executor_worker import ExecutorWorker
        worker = ExecutorWorker.__new__(ExecutorWorker)
        return worker

    def test_prefers_full_top_level_json_over_nested_object(self):
        worker = self._make_worker()

        class _Session:
            stdout = (
                '{"schema_version":"v1","reply":"ok","actions":[{"type":"reply_only"}],'
                '"context_update":{"current_focus":"python_compatibility_fix","last_decision":"reply_only"}}'
            )
            exit_code = 0

        parsed = worker._parse_output(_Session(), "coordinator")
        self.assertEqual(parsed.get("schema_version"), "v1")
        self.assertEqual(parsed.get("reply"), "ok")
        self.assertEqual(parsed.get("actions", [{}])[0].get("type"), "reply_only")

    def test_markdown_json_block_still_parses(self):
        worker = self._make_worker()

        class _Session:
            stdout = '```json\n{"schema_version":"v1","reply":"ok","actions":[{"type":"reply_only"}]}\n```'
            exit_code = 0

        parsed = worker._parse_output(_Session(), "coordinator")
        self.assertEqual(parsed.get("schema_version"), "v1")


class TestPythonCompatibilityAnnotations(unittest.TestCase):
    """Guard against PEP 604 unions in runtime-evaluated target modules."""

    def test_executor_worker_avoids_pep604_optional_annotations(self):
        content = Path(os.path.join(agent_dir, "executor_worker.py")).read_text(encoding="utf-8")
        self.assertNotIn("-> dict | None", content)
        self.assertNotIn("-> str | None", content)
        self.assertNotIn(": str | None", content)


if __name__ == "__main__":
    unittest.main()
