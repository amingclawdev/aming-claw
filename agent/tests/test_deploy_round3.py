"""Tests for deploy semantics structural coherence (R1-R6, AC1-AC5).

Round 3 tests verifying:
  (a) non-gateway deploy returns gateway='not_applicable' and all_pass=True
  (b) report.success=True with smoke_test.all_pass=False is impossible
  (c) _gate_deploy_pass rejects incoherent reports
  (d) gateway deploy includes gateway in all_pass
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)


# ---------------------------------------------------------------------------
# AC5(a): Non-gateway deploy returns gateway='not_applicable' and all_pass=True
# ---------------------------------------------------------------------------


class TestNonGatewayDeployNotApplicable(unittest.TestCase):
    """AC1/R1/R6: Non-affected services marked 'not_applicable', not False."""

    def test_smoke_test_marks_non_affected_as_not_applicable(self):
        """smoke_test() with affected_services=['governance'] must mark
        executor and gateway as 'not_applicable'."""
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess"), \
             mock.patch("requests.get", return_value=mock.Mock(status_code=200)), \
             mock.patch("time.sleep"):
            result = smoke_test(affected_services=["governance"])

        self.assertEqual(result["gateway"], "not_applicable")
        self.assertEqual(result["executor"], "not_applicable")
        self.assertTrue(result["governance"])
        self.assertTrue(result["all_pass"])

    def test_run_deploy_non_gateway_returns_not_applicable(self):
        """run_deploy for governance-only files must have gateway='not_applicable'
        in smoke_test and success=True when all steps pass."""
        from deploy_chain import run_deploy

        fake_smoke = {
            "executor": "not_applicable",
            "governance": True,
            "gateway": "not_applicable",
            "all_pass": True,
        }
        with mock.patch("deploy_chain.detect_affected_services", return_value=["governance"]), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(True, "ok")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/governance/server.py"])

        self.assertTrue(report["success"])
        self.assertEqual(report["smoke_test"]["gateway"], "not_applicable")
        self.assertTrue(report["smoke_test"]["all_pass"])

    def test_gateway_false_never_appears_for_non_gateway_deploy(self):
        """gateway=False must never appear for deploys not touching gateway files."""
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess"), \
             mock.patch("requests.get", return_value=mock.Mock(status_code=200)), \
             mock.patch("time.sleep"):
            result = smoke_test(affected_services=["executor"])

        # gateway must be 'not_applicable', never False
        self.assertEqual(result["gateway"], "not_applicable")
        self.assertIsNot(result["gateway"], False)


# ---------------------------------------------------------------------------
# AC5(b): report.success=True with smoke_test.all_pass=False is impossible
# ---------------------------------------------------------------------------


class TestSuccessAllPassCoherence(unittest.TestCase):
    """AC2/R2: success derived from single expression; no coherence_override."""

    def test_success_false_when_smoke_all_pass_false(self):
        """run_deploy must return success=False when smoke_test.all_pass=False,
        even if all deploy steps succeed."""
        from deploy_chain import run_deploy

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

        self.assertFalse(report["success"],
                         "success=True with all_pass=False is impossible")
        # AC2: no coherence_override key should exist
        self.assertNotIn("coherence_override", report,
                         "coherence_override pattern must be removed")

    def test_no_coherence_override_in_deploy_chain(self):
        """AC2: grep for 'coherence_override' finds zero matches in deploy_chain.py."""
        deploy_chain_path = os.path.join(agent_dir, "deploy_chain.py")
        with open(deploy_chain_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("coherence_override", content,
                         "deploy_chain.py must not contain 'coherence_override'")

    def test_success_single_derivation(self):
        """R2: success assigned from single expression combining steps + all_pass."""
        from deploy_chain import run_deploy

        # Step fails, smoke passes → success must be False
        fake_smoke = {
            "executor": "not_applicable",
            "governance": True,
            "gateway": "not_applicable",
            "all_pass": True,
        }
        with mock.patch("deploy_chain.detect_affected_services", return_value=["governance"]), \
             mock.patch("deploy_chain.rebuild_governance", return_value=(False, "fail")), \
             mock.patch("deploy_chain.restart_local_governance", return_value=(False, "also fail")), \
             mock.patch("deploy_chain.smoke_test", return_value=fake_smoke), \
             mock.patch("deploy_chain._save_report"):
            report = run_deploy(["agent/governance/server.py"])
        self.assertFalse(report["success"],
                         "success must be False when a step fails")


# ---------------------------------------------------------------------------
# AC5(c): _gate_deploy_pass rejects incoherent reports
# ---------------------------------------------------------------------------


class TestGateDeployPassSemantics(unittest.TestCase):
    """AC3/R3: _gate_deploy_pass validates smoke_test semantics."""

    def test_gate_rejects_when_all_pass_false(self):
        """Gate must reject when smoke_test.all_pass=False even if success=True."""
        from governance.auto_chain import _gate_deploy_pass

        result = {
            "report": {
                "success": True,
                "smoke_test": {
                    "executor": True,
                    "governance": True,
                    "gateway": False,
                    "all_pass": False,
                },
            }
        }
        passed, reason = _gate_deploy_pass(None, "test", result, {})
        self.assertFalse(passed, "gate must reject when all_pass=False")
        self.assertIn("all_pass", reason)

    def test_gate_rejects_when_service_false(self):
        """Gate must reject when any individual service=False."""
        from governance.auto_chain import _gate_deploy_pass

        result = {
            "report": {
                "success": True,
                "smoke_test": {
                    "executor": True,
                    "governance": True,
                    "gateway": False,
                    "all_pass": True,  # Even if all_pass is True (inconsistent)
                },
            }
        }
        passed, reason = _gate_deploy_pass(None, "test", result, {})
        self.assertFalse(passed, "gate must reject when a service=False")
        self.assertIn("gateway", reason)

    def test_gate_accepts_coherent_success(self):
        """Gate accepts when success=True and smoke_test is clean."""
        from governance.auto_chain import _gate_deploy_pass

        result = {
            "report": {
                "success": True,
                "smoke_test": {
                    "executor": True,
                    "governance": True,
                    "gateway": "not_applicable",
                    "all_pass": True,
                },
            }
        }
        passed, reason = _gate_deploy_pass(None, "test", result, {})
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")

    def test_gate_rejects_failed_report(self):
        """Gate rejects when report.success=False."""
        from governance.auto_chain import _gate_deploy_pass

        result = {"report": {"success": False}}
        passed, reason = _gate_deploy_pass(None, "test", result, {})
        self.assertFalse(passed)


# ---------------------------------------------------------------------------
# AC5(d): Gateway deploy includes gateway in all_pass
# ---------------------------------------------------------------------------


class TestGatewayDeployFullParticipation(unittest.TestCase):
    """R5: Gateway deploys include gateway in smoke_test and all_pass."""

    def test_gateway_deploy_includes_gateway_in_smoke(self):
        """When gateway is in affected_services, it must be actively checked."""
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess") as mock_sp, \
             mock.patch("requests.get", return_value=mock.Mock(status_code=200)), \
             mock.patch("time.sleep"):
            mock_sp.run.return_value = mock.Mock(stdout="true\n", returncode=0)
            result = smoke_test(affected_services=["executor", "governance", "gateway"])

        # gateway must be a bool, not 'not_applicable'
        self.assertIsInstance(result["gateway"], bool)
        self.assertTrue(result["gateway"])
        self.assertTrue(result["all_pass"])

    def test_gateway_failure_causes_all_pass_false(self):
        """When gateway smoke fails, all_pass must be False."""
        from deploy_chain import smoke_test

        with mock.patch("deploy_chain.subprocess") as mock_sp, \
             mock.patch("requests.get", return_value=mock.Mock(status_code=200)), \
             mock.patch("time.sleep"):
            mock_sp.run.return_value = mock.Mock(stdout="false\n", returncode=0)
            result = smoke_test(affected_services=["executor", "governance", "gateway"])

        self.assertFalse(result["gateway"])
        self.assertFalse(result["all_pass"])


# ---------------------------------------------------------------------------
# AC4: executor_worker coherence check
# ---------------------------------------------------------------------------


class TestExecutorWorkerCoherence(unittest.TestCase):
    """R4: executor_worker._execute_deploy has runtime coherence assertion."""

    def test_coherence_check_forces_failure(self):
        """_execute_deploy must force status='failed' when report.success=True
        but smoke_test.all_pass=False."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker(
            "aming-claw",
            governance_url="http://localhost:40000",
            workspace=os.getcwd(),
        )
        metadata = {"changed_files": ["agent/telegram_gateway/bot.py"]}

        incoherent_report = {
            "success": True,
            "affected_services": ["gateway"],
            "smoke_test": {
                "executor": "not_applicable",
                "governance": "not_applicable",
                "gateway": False,
                "all_pass": False,
            },
        }
        with mock.patch.object(worker, "_report_progress"), \
             mock.patch("deploy_chain.run_deploy", return_value=incoherent_report):
            out = worker._execute_deploy("task-deploy-coherence", metadata)

        self.assertEqual(out["status"], "failed",
                         "executor_worker must reject incoherent report")
        self.assertTrue(out["result"]["report"].get("coherence_violation"),
                        "coherence_violation flag must be set")

    def test_coherence_check_passes_for_valid_report(self):
        """_execute_deploy returns succeeded for coherent success=True reports."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker(
            "aming-claw",
            governance_url="http://localhost:40000",
            workspace=os.getcwd(),
        )
        metadata = {"changed_files": ["agent/executor_worker.py"]}

        good_report = {
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
             mock.patch("deploy_chain.run_deploy", return_value=good_report):
            out = worker._execute_deploy("task-deploy-ok", metadata)

        self.assertEqual(out["status"], "succeeded")

    def test_coherence_check_catches_service_false(self):
        """_execute_deploy detects individual service=False even if all_pass=True."""
        from executor_worker import ExecutorWorker

        worker = ExecutorWorker(
            "aming-claw",
            governance_url="http://localhost:40000",
            workspace=os.getcwd(),
        )
        metadata = {"changed_files": ["agent/telegram_gateway/bot.py"]}

        bad_report = {
            "success": True,
            "affected_services": ["gateway"],
            "smoke_test": {
                "executor": "not_applicable",
                "governance": "not_applicable",
                "gateway": False,
                "all_pass": True,  # inconsistent
            },
        }
        with mock.patch.object(worker, "_report_progress"), \
             mock.patch("deploy_chain.run_deploy", return_value=bad_report):
            out = worker._execute_deploy("task-deploy-svc-false", metadata)

        self.assertEqual(out["status"], "failed")


# ---------------------------------------------------------------------------
# Existing tests (preserved and updated)
# ---------------------------------------------------------------------------


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
             mock.patch("deploy_chain.run_deploy", return_value={
                 "success": True,
                 "affected_services": ["executor"],
                 "smoke_test": {"executor": True, "governance": "not_applicable",
                                "gateway": "not_applicable", "all_pass": True},
             }):
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


if __name__ == "__main__":
    unittest.main()
