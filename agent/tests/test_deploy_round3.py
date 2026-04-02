import json
import os
import sys
import tempfile
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


class TestDeployStageRound3(unittest.TestCase):
    def test_merge_builds_followup_deploy_task(self):
        from governance.auto_chain import _build_deploy_prompt

        result = {
            "merge_commit": "abc123",
            "changed_files": ["agent/executor_worker.py"],
        }
        metadata = {
            "changed_files": ["agent/executor_worker.py"],
            "related_nodes": ["L4.41"],
        }

        prompt, out_meta = _build_deploy_prompt("task-merge-1", result, metadata)

        self.assertIn("Deploy changes after merge task task-merge-1", prompt)
        self.assertEqual(out_meta["merge_commit"], "abc123")
        self.assertEqual(out_meta["changed_files"], ["agent/executor_worker.py"])
        self.assertEqual(out_meta["related_nodes"], ["L4.41"])

    def test_host_deploy_returns_succeeded_when_report_successful(self):
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw", governance_url="http://localhost:40000", workspace=os.getcwd())
        metadata = {"changed_files": ["agent/executor_worker.py"]}

        with mock.patch.object(worker, "_report_progress"), \
             mock.patch("deploy_chain.run_deploy", return_value={"success": True, "affected_services": ["executor"]}):
            out = worker._execute_deploy("task-deploy-1", metadata)

        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["result"]["deploy"], "completed")


class TestDeploySmokeFallbackRound3(unittest.TestCase):
    def test_executor_health_falls_back_to_manager_status_file(self):
        from deploy_chain import _executor_health_from_state

        with tempfile.TemporaryDirectory() as td:
            state_dir = os.path.join(td, "codex-tasks", "state")
            os.makedirs(state_dir, exist_ok=True)
            status_path = os.path.join(state_dir, "manager_status.json")
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump({"services": {"executor": "running", "manager": "running"}}, f)

            with mock.patch.dict(os.environ, {"SHARED_VOLUME_PATH": td}):
                self.assertTrue(_executor_health_from_state())


class TestDeploySemanticsReconciliation(unittest.TestCase):
    """Tests for deploy result semantics reconciliation (R1-R7)."""

    # AC6: governance-only deploy skips gateway smoke, all_pass=True
    def test_governance_only_deploy_skips_gateway_smoke(self):
        from deploy_chain import smoke_test

        # Mock all HTTP/subprocess calls so only governance is checked
        with mock.patch("deploy_chain.subprocess") as mock_sp, \
             mock.patch("requests.get") as mock_get:
            # governance health returns 200
            mock_get.return_value = mock.Mock(status_code=200)
            mock.patch("time.sleep").start()

            result = smoke_test(affected_services=["governance"])

        self.assertEqual(result["gateway"], "skipped")
        self.assertEqual(result["executor"], "skipped")
        self.assertTrue(result["all_pass"])

    # AC7: report.success matches smoke_test.all_pass
    def test_report_success_consistent_with_smoke(self):
        from deploy_chain import run_deploy

        # Case 1: smoke all_pass=False → report.success=False
        fake_smoke_fail = {"executor": True, "governance": False, "gateway": "skipped", "all_pass": False}
        with mock.patch("deploy_chain.detect_affected_services", return_value=["governance"]), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(True, "ok")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke_fail), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/governance/server.py"])
        self.assertFalse(report["success"])

        # Case 2: smoke all_pass=True + step success → report.success=True
        fake_smoke_pass = {"executor": "skipped", "governance": True, "gateway": "skipped", "all_pass": True}
        with mock.patch("deploy_chain.detect_affected_services", return_value=["governance"]), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(True, "ok")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke_pass), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/governance/server.py"])
        self.assertTrue(report["success"])

        # Case 3: step fails but smoke passes → report.success=False (R1/R6)
        fake_smoke_pass2 = {"executor": "skipped", "governance": True, "gateway": "skipped", "all_pass": True}
        with mock.patch("deploy_chain.detect_affected_services", return_value=["governance"]), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(False, "fail")), \
             mock.patch("deploy_chain.restart_local_governance", return_value=(False, "also fail")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke_pass2), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/governance/server.py"])
        self.assertFalse(report["success"])

    # AC8: gateway deploy triggers full smoke (gateway is checked, not skipped)
    def test_gateway_deploy_triggers_full_smoke(self):
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess") as mock_sp, \
             mock.patch("requests.get") as mock_get:
            mock_get.return_value = mock.Mock(status_code=200)
            mock_sp.run.return_value = mock.Mock(stdout="true\n", returncode=0)
            mock.patch("time.sleep").start()

            result = smoke_test(affected_services=["executor", "governance", "gateway"])

        # gateway should be checked (bool), not skipped
        self.assertIsInstance(result["gateway"], bool)
        self.assertNotEqual(result["gateway"], "skipped")

    # AC9: restart_local_governance uses port 40000 by default
    def test_host_governance_port_40000(self):
        import inspect
        from deploy_chain import restart_local_governance

        sig = inspect.signature(restart_local_governance)
        default_port = sig.parameters["port"].default
        self.assertEqual(default_port, 40000)

    # AC5: rebuild_governance detects host-runtime and skips Docker
    def test_rebuild_governance_host_runtime_skips_docker(self):
        from deploy_chain import rebuild_governance

        with mock.patch("deploy_chain._is_host_runtime_mode", return_value=True), \
             mock.patch("deploy_chain.restart_local_governance", return_value=(True, "local ok")) as mock_local:
            ok, summary = rebuild_governance()

        mock_local.assert_called_once_with(port=40000)
        self.assertTrue(ok)
        self.assertEqual(summary, "local ok")


class TestDeployCoherenceInvariant(unittest.TestCase):
    """Tests for deploy result coherence invariant (AC3-AC6, R1-R7)."""

    # AC3: test_coherence_violation_impossible — the exact mismatch scenario
    # (success=true, all_pass=false, gateway=false) must be impossible.
    def test_coherence_violation_impossible(self):
        """Construct the exact failure scenario and assert coherence invariant
        prevents success=True when all_pass=False and gateway=False."""
        from deploy_chain import run_deploy

        # Simulate: all steps succeed but smoke_test fails (gateway=False, all_pass=False)
        fake_smoke = {
            "executor": True,
            "governance": True,
            "gateway": False,
            "all_pass": False,
        }
        with mock.patch("deploy_chain.detect_affected_services",
                         return_value=["executor", "governance", "gateway"]), \
             mock.patch("deploy_chain.restart_executor", return_value=True), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(True, "ok")), \
             mock.patch("deploy_chain.restart_gateway", return_value=(True, "ok")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/telegram_gateway/bot.py"])

        # The coherence invariant must force success=False
        self.assertFalse(report["success"],
                         "Coherence violation: success=True while all_pass=False is forbidden")
        # Verify the exact scenario is what we constructed
        self.assertFalse(report["smoke_test"]["all_pass"])
        self.assertFalse(report["smoke_test"]["gateway"])

    # AC4: test_success_false_when_nonskipped_smoke_fails
    def test_success_false_when_nonskipped_smoke_fails(self):
        """Assert success=False when any non-skipped service smoke returns False,
        even if all deploy steps succeed."""
        from deploy_chain import run_deploy

        # governance smoke fails, gateway skipped, executor skipped
        fake_smoke = {
            "executor": "skipped",
            "governance": False,
            "gateway": "skipped",
            "all_pass": False,
        }
        with mock.patch("deploy_chain.detect_affected_services",
                         return_value=["governance"]), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(True, "ok")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/governance/server.py"])

        self.assertFalse(report["success"],
                         "success must be False when any non-skipped smoke test fails")

    # AC5: test_gateway_skipped_for_governance_only_deploy
    def test_gateway_skipped_for_governance_only_deploy(self):
        """Assert gateway='skipped' when changed_files only touch agent/governance/** paths."""
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess") as mock_sp, \
             mock.patch("requests.get") as mock_get:
            mock_get.return_value = mock.Mock(status_code=200)
            mock.patch("time.sleep").start()

            result = smoke_test(affected_services=["governance"])

        self.assertEqual(result["gateway"], "skipped",
                         "gateway must be 'skipped' for governance-only deploys")

    # AC6: test_gateway_checked_for_gateway_deploy
    def test_gateway_checked_for_gateway_deploy(self):
        """Assert gateway is a bool (not 'skipped') when changed_files include
        agent/telegram_gateway/** paths."""
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess") as mock_sp, \
             mock.patch("requests.get") as mock_get:
            mock_get.return_value = mock.Mock(status_code=200)
            mock_sp.run.return_value = mock.Mock(stdout="true\n", returncode=0)
            mock.patch("time.sleep").start()

            result = smoke_test(affected_services=["executor", "governance", "gateway"])

        self.assertIsInstance(result["gateway"], bool,
                             "gateway must be a bool (actively checked) for gateway deploys")
        self.assertNotEqual(result["gateway"], "skipped")

    # AC2-supplement: executor_worker coherence check also catches mismatch
    def test_executor_worker_coherence_check(self):
        """Verify _execute_deploy in executor_worker forces failure when
        report.success=True but smoke_test.all_pass=False."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker("aming-claw",
                                governance_url="http://localhost:40000",
                                workspace=os.getcwd())
        metadata = {"changed_files": ["agent/telegram_gateway/bot.py"]}

        # Simulate run_deploy returning an incoherent result
        incoherent_report = {
            "success": True,
            "affected_services": ["gateway"],
            "smoke_test": {
                "executor": "skipped",
                "governance": "skipped",
                "gateway": False,
                "all_pass": False,
            },
        }
        with mock.patch.object(worker, "_report_progress"), \
             mock.patch("deploy_chain.run_deploy", return_value=incoherent_report):
            out = worker._execute_deploy("task-deploy-2", metadata)

        # executor_worker coherence check must force failure
        self.assertEqual(out["status"], "failed",
                         "executor_worker must reject incoherent report (success=True, all_pass=False)")


if __name__ == "__main__":
    unittest.main()
