"""Tests for chain context event-sourced store."""

import json
import os
import sqlite3
import sys
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.chain_context import (
    ChainContextStore, _extract_core, RESULT_CORE_FIELDS,
)


class TestExtractCore(unittest.TestCase):
    def test_extracts_known_fields(self):
        result = {
            "target_files": ["a.py"],
            "changed_files": ["b.py"],
            "summary": "test",
            "extra_field": "ignored",
        }
        core = _extract_core(result)
        self.assertEqual(core["target_files"], ["a.py"])
        self.assertEqual(core["summary"], "test")
        self.assertNotIn("extra_field", core)

    def test_extracts_from_prd_nested(self):
        result = {"prd": {"target_files": ["c.py"], "verification": {"test": True}}}
        core = _extract_core(result)
        self.assertEqual(core["target_files"], ["c.py"])
        self.assertEqual(core["verification"], {"test": True})

    def test_empty_result(self):
        self.assertEqual(_extract_core({}), {})
        self.assertEqual(_extract_core(None), {})


class TestChainContextStore(unittest.TestCase):
    def setUp(self):
        self.store = ChainContextStore()
        self.pid = "test-proj"

    def _create_task(self, task_id, task_type="pm", parent=None, prompt="test prompt"):
        self.store.on_task_created({
            "task_id": task_id,
            "type": task_type,
            "prompt": prompt,
            "parent_task_id": parent or "",
            "project_id": self.pid,
        })

    def _complete_task(self, task_id, result=None):
        self.store.on_task_completed({
            "task_id": task_id,
            "result": result or {},
            "project_id": self.pid,
        })

    def test_create_chain(self):
        self._create_task("t1")
        chain = self.store.get_chain("t1")
        self.assertIsNotNone(chain)
        self.assertEqual(chain["state"], "running")
        self.assertEqual(chain["stage_count"], 1)

    def test_chain_progression(self):
        self._create_task("t1", "pm")
        self._complete_task("t1", {"target_files": ["a.py"]})
        self._create_task("t2", "dev", parent="t1")
        chain = self.store.get_chain("t2")
        self.assertEqual(chain["stage_count"], 2)
        self.assertEqual(chain["current_stage"], "t2")

    def test_get_original_prompt(self):
        self._create_task("t1", "pm", prompt="Build feature X")
        self._create_task("t2", "dev", parent="t1", prompt="Implement X")
        prompt = self.store.get_original_prompt("t2")
        self.assertEqual(prompt, "Build feature X")

    def test_get_parent_result(self):
        self._create_task("t1", "pm")
        self._complete_task("t1", {"target_files": ["a.py"], "verification": {"test": True}})
        self._create_task("t2", "dev", parent="t1")
        parent_result = self.store.get_parent_result("t2")
        self.assertIsNotNone(parent_result)
        self.assertEqual(parent_result["target_files"], ["a.py"])

    def test_get_parent_result_none_for_root(self):
        self._create_task("t1", "pm")
        self.assertIsNone(self.store.get_parent_result("t1"))

    def test_gate_blocked(self):
        self._create_task("t1", "dev")
        self.store.on_gate_blocked({
            "task_id": "t1", "reason": "missing docs",
            "project_id": self.pid,
        })
        self.assertEqual(self.store.get_state("t1"), "blocked")
        chain = self.store.get_chain("t1")
        blocked_stage = [s for s in chain["stages"] if s["task_id"] == "t1"][0]
        self.assertEqual(blocked_stage["gate_reason"], "missing docs")

    def test_retry_flow(self):
        self._create_task("t1", "dev")
        self.store.on_gate_blocked({
            "task_id": "t1", "reason": "test fail",
            "project_id": self.pid,
        })
        self.store.on_task_retry({
            "task_id": "t2", "original_task_id": "t1",
            "project_id": self.pid,
        })
        self.assertEqual(self.store.get_state("t1"), "retrying")
        chain = self.store.get_chain("t2")
        retry_stage = [s for s in chain["stages"] if s["task_id"] == "t2"][0]
        self.assertEqual(retry_stage["attempt"], 2)

    def test_failed_auto_archives(self):
        self._create_task("t1", "dev")
        self.store.on_task_failed({
            "task_id": "t1", "reason": "retry_exhausted",
            "project_id": self.pid,
        })
        # Chain should be archived (released from memory)
        self.assertIsNone(self.store.get_chain("t1"))
        self.assertIsNone(self.store.get_state("t1"))

    def test_merge_completes_chain(self):
        self._create_task("t1", "merge")
        self._complete_task("t1")
        self.assertEqual(self.store.get_state("t1"), "completed")

    def test_archive_releases_memory(self):
        self._create_task("t1", "pm")
        self._create_task("t2", "dev", parent="t1")
        self.store.archive_chain("t2", self.pid)
        self.assertIsNone(self.store.get_chain("t1"))
        self.assertIsNone(self.store.get_chain("t2"))

    def test_role_filtering(self):
        self._create_task("t1", "pm", prompt="secret PRD")
        self._complete_task("t1", {"target_files": ["a.py"], "summary": "done"})
        self._create_task("t2", "dev", parent="t1")

        # test role: should only see dev and test stages, not pm
        chain = self.store.get_chain("t2", role="test")
        visible_types = {s["type"] for s in chain["stages"]}
        self.assertIn("dev", visible_types)
        self.assertNotIn("pm", visible_types)

        # coordinator: sees everything
        chain = self.store.get_chain("t2", role="coordinator")
        visible_types = {s["type"] for s in chain["stages"]}
        self.assertIn("pm", visible_types)
        self.assertIn("dev", visible_types)

    def test_idempotent_create(self):
        self._create_task("t1")
        self._create_task("t1")  # duplicate
        chain = self.store.get_chain("t1")
        self.assertEqual(chain["stage_count"], 1)

    def test_unknown_task_returns_none(self):
        self.assertIsNone(self.store.get_chain("nonexistent"))
        self.assertIsNone(self.store.get_state("nonexistent"))
        self.assertEqual(self.store.get_original_prompt("nonexistent"), "")
        self.assertIsNone(self.store.get_parent_result("nonexistent"))


class TestChainContextRecovery(unittest.TestCase):
    """Test crash recovery by simulating DB events."""

    def test_recovery_rebuilds_state(self):
        # Create events in a fresh store, then recover in a new store
        store1 = ChainContextStore()
        store1._recovering = True  # suppress DB writes

        store1.on_task_created({
            "task_id": "r1", "type": "pm", "prompt": "build it",
            "parent_task_id": "", "project_id": "proj",
        })
        store1.on_task_completed({
            "task_id": "r1", "result": {"target_files": ["x.py"]},
            "project_id": "proj",
        })
        store1.on_task_created({
            "task_id": "r2", "type": "dev", "prompt": "implement",
            "parent_task_id": "r1", "project_id": "proj",
        })
        store1._recovering = False

        # Verify state was built
        chain = store1.get_chain("r2")
        self.assertIsNotNone(chain)
        self.assertEqual(chain["stage_count"], 2)
        self.assertEqual(store1.get_original_prompt("r2"), "build it")


class TestGetAccumulatedChangedFiles(unittest.TestCase):
    """B28a: ChainContextStore.get_accumulated_changed_files accessor."""

    def setUp(self):
        self.store = ChainContextStore()

    def test_returns_union_of_dev_changed_files(self):
        """Accumulates changed_files from multiple dev stages."""
        for tid, ttype, parent in [
            ("t-pm", "pm", ""),
            ("t-dev1", "dev", "t-pm"),
            ("t-dev2", "dev", "t-dev1"),
        ]:
            self.store.on_task_created({
                "task_id": tid, "type": ttype,
                "parent_task_id": parent, "prompt": "p", "project_id": "proj",
            })
        self.store.on_task_completed({
            "task_id": "t-dev1", "type": "dev",
            "result": {"changed_files": ["agent/foo.py", "docs/api/x.md"]},
            "project_id": "proj",
        })
        self.store.on_task_completed({
            "task_id": "t-dev2", "type": "dev",
            "result": {"changed_files": ["agent/bar.py", "agent/foo.py"]},
            "project_id": "proj",
        })
        # chain_id is the root task id
        files = self.store.get_accumulated_changed_files("t-pm", "proj")
        self.assertIn("agent/foo.py", files)
        self.assertIn("agent/bar.py", files)
        self.assertIn("docs/api/x.md", files)
        self.assertEqual(len(files), 3)  # deduped

    def test_returns_empty_for_unknown_chain(self):
        files = self.store.get_accumulated_changed_files("unknown-chain", "")
        self.assertEqual(files, [])


class TestGetRetryScope(unittest.TestCase):
    """B28a: ChainContextStore.get_retry_scope accessor."""

    def setUp(self):
        self.store = ChainContextStore()

    def _build_chain_with_dev(self, dev_changed):
        for tid, ttype, parent in [
            ("r-pm", "pm", ""),
            ("r-dev", "dev", "r-pm"),
        ]:
            self.store.on_task_created({
                "task_id": tid, "type": ttype,
                "parent_task_id": parent, "prompt": "p", "project_id": "proj",
            })
        self.store.on_task_completed({
            "task_id": "r-dev", "type": "dev",
            "result": {"changed_files": dev_changed},
            "project_id": "proj",
        })

    def test_combines_pm_metadata_with_dev_changed(self):
        """retry scope = PM target_files + prev dev changed_files."""
        self._build_chain_with_dev(["config/roles/dev.yaml", "docs/api/x.md"])
        base_metadata = {
            "target_files": ["agent/executor_worker.py"],
            "test_files": ["agent/tests/test_foo.py"],
            "doc_impact": {"files": ["docs/api/executor-api.md"]},
        }
        scope = self.store.get_retry_scope("r-pm", "proj", base_metadata)
        self.assertIn("agent/executor_worker.py", scope)
        self.assertIn("agent/tests/test_foo.py", scope)
        self.assertIn("docs/api/executor-api.md", scope)
        self.assertIn("config/roles/dev.yaml", scope)
        self.assertIn("docs/api/x.md", scope)

    def test_empty_dev_history_returns_pm_only(self):
        """No prev dev → scope equals PM metadata only."""
        for tid, ttype, parent in [("n-pm", "pm", "")]:
            self.store.on_task_created({
                "task_id": tid, "type": ttype,
                "parent_task_id": parent, "prompt": "p", "project_id": "proj",
            })
        base_metadata = {"target_files": ["agent/foo.py"], "test_files": [], "doc_impact": {}}
        scope = self.store.get_retry_scope("n-pm", "proj", base_metadata)
        self.assertEqual(scope, {"agent/foo.py"})

    def test_additive_only_never_removes_pm_files(self):
        """Dev changed_files only adds to scope, never removes PM files."""
        self._build_chain_with_dev(["config/roles/dev.yaml"])
        base_metadata = {"target_files": ["agent/executor_worker.py"]}
        scope = self.store.get_retry_scope("r-pm", "proj", base_metadata)
        self.assertIn("agent/executor_worker.py", scope)  # PM file preserved
        self.assertIn("config/roles/dev.yaml", scope)    # dev file added


class TestGetLatestTestReport(unittest.TestCase):
    """B28b: ChainContextStore.get_latest_test_report accessor."""

    def setUp(self):
        self.store = ChainContextStore()

    def _build_chain_with_test_report(self, test_report):
        """Build a pm→dev→test→qa chain with test_report on the test stage."""
        for tid, ttype, parent in [
            ("t-pm", "pm", ""),
            ("t-dev", "dev", "t-pm"),
            ("t-test", "test", "t-dev"),
            ("t-qa", "qa", "t-test"),
        ]:
            self.store.on_task_created({
                "task_id": tid, "type": ttype,
                "parent_task_id": parent, "prompt": "p", "project_id": "proj",
            })
        self.store.on_task_completed({
            "task_id": "t-test", "type": "test",
            "result": {"test_report": test_report, "summary": "ok"},
            "project_id": "proj",
        })

    def test_returns_test_report_for_qa_task(self):
        """get_latest_test_report returns test_report when QA is in-chain."""
        tr = {"passed": 10, "failed": 0, "tool": "pytest"}
        self._build_chain_with_test_report(tr)
        result = self.store.get_latest_test_report("t-qa")
        self.assertEqual(result, tr)

    def test_returns_none_when_no_test_stage_completed(self):
        """get_latest_test_report returns None when test stage not yet completed."""
        self.store.on_task_created({
            "task_id": "t-pm2", "type": "pm",
            "parent_task_id": "", "prompt": "p", "project_id": "proj",
        })
        result = self.store.get_latest_test_report("t-pm2")
        self.assertIsNone(result)

    def test_returns_none_for_unknown_task_id(self):
        """get_latest_test_report returns None for task_id not in memory (no project_id)."""
        result = self.store.get_latest_test_report("unknown-task-xyz")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
