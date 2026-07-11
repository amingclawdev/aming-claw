import os
import sys
import tempfile
import unittest
import io
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAILifecycleProviderRouting(unittest.TestCase):
    def test_agent_run_rejects_conflicting_caller_route_before_dispatch(self):
        from ai_invocation import (
            AIInvocationResult,
            RoutePromptContract,
            invoke_agent_run,
        )
        from cli_agent_service.config import resolve_agent_config

        run = resolve_agent_config(
            run_id="run-pinned-route",
            role="dev",
            project_id="aming-claw",
            compatibility_defaults={
                "provider": "openai",
                "model": "gpt-5.4-codex",
                "backend_mode": "codex_cli",
                "auth_mode": "cli_auth",
            },
            governance_refs={
                "route_id": "route-pinned",
                "route_context_hash": "sha256:" + "1" * 64,
                "prompt_contract_id": "rprompt-pinned",
                "prompt_contract_hash": "sha256:" + "2" * 64,
                "route_token_ref": "rtok-" + "3" * 32,
            },
        )

        with patch("ai_invocation.invoke_cli") as invoke_cli:
            with self.assertRaisesRegex(ValueError, "route_id"):
                invoke_agent_run(
                    run,
                    prompt="must not dispatch",
                    route=RoutePromptContract(route_id="route-conflicting"),
                )
        invoke_cli.assert_not_called()

        with patch("ai_invocation.invoke_cli") as invoke_cli:
            invoke_cli.side_effect = lambda request: AIInvocationResult(
                request=request,
                status="completed",
            )
            result = invoke_agent_run(
                run,
                prompt="matching route dispatch",
                route=RoutePromptContract(route_id="route-pinned"),
            )
        self.assertEqual(result.request.route.route_id, "route-pinned")
        self.assertEqual(
            result.request.route.route_context_hash,
            "sha256:" + "1" * 64,
        )
        invoke_cli.assert_called_once()

    def test_agent_run_adapter_dispatches_cli_api_and_external_local_routes(self):
        from ai_invocation import AIInvocationResult, invoke_agent_run
        from cli_agent_service.config import resolve_agent_config
        from cli_agent_service.models import (
            AgentProfile,
            CredentialRef,
            HarnessRuntime,
            InferenceEndpoint,
            LauncherAdapter,
            RolePolicy,
        )

        def profile(profile_id, provider, model, backend_mode, auth_mode):
            return AgentProfile(
                profile_id=profile_id,
                harness_runtime=HarnessRuntime(runtime_id="runtime-" + profile_id),
                inference_endpoint=InferenceEndpoint(
                    endpoint_id="endpoint-" + profile_id,
                    provider=provider,
                    model=model,
                    backend_mode=backend_mode,
                    auth_mode=auth_mode,
                ),
                credential_ref=CredentialRef(
                    ref_id="credential:provider-home:" + profile_id,
                    provider=provider,
                ),
                launcher_adapter=LauncherAdapter(launcher_id="launcher-" + profile_id),
                role_policy=RolePolicy(policy_id="policy-" + profile_id, roles=("dev",)),
            )

        cli_run = resolve_agent_config(
            run_id="run-cli",
            role="dev",
            project_id="aming-claw",
            profile=profile("cli", "openai", "gpt-5.4-codex", "codex_cli", "cli_auth"),
        )
        with patch("ai_invocation.invoke_cli") as invoke_cli:
            invoke_cli.side_effect = lambda request: AIInvocationResult(
                request=request,
                status="completed",
            )
            cli_result = invoke_agent_run(cli_run, prompt="cli prompt")
        self.assertEqual(cli_result.request.backend_mode, "codex_cli")
        invoke_cli.assert_called_once()

        api_run = resolve_agent_config(
            run_id="run-api",
            role="dev",
            project_id="aming-claw",
            profile=profile("api", "openai", "gpt-4o", "openai_api", "api_key_env"),
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            api_result = invoke_agent_run(api_run, prompt="api prompt")
        self.assertEqual(api_result.auth_status, "missing_api_key")

        local_run = resolve_agent_config(
            run_id="run-local",
            role="dev",
            project_id="aming-claw",
            profile=profile(
                "local",
                "openai",
                "local-c0-model",
                "docker_live_ai",
                "external_harness",
            ),
        )
        local_result = invoke_agent_run(local_run, prompt="local prompt")
        self.assertEqual(local_result.auth_status, "external_harness_required")

    def test_pipeline_routing_carries_backend_auth_and_output_policy(self):
        from pipeline_config import resolve_role_config

        resolved = resolve_role_config(
            "dev",
            {
                "default": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "backend_mode": "claude_cli",
                    "auth_mode": "cli_auth",
                    "output_policy": "hash_and_summary_only",
                },
                "roles": {
                    "dev": {
                        "provider": "openai",
                        "model": "gpt-4o",
                        "backend_mode": "openai_api",
                        "auth_mode": "api_key_env",
                    }
                },
            },
        )

        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["backend_mode"], "openai_api")
        self.assertEqual(resolved["auth_mode"], "api_key_env")
        self.assertEqual(resolved["output_policy"], "hash_and_summary_only")

    def test_loaded_role_without_output_policy_inherits_default(self):
        from pipeline_config import load_pipeline_config, resolve_role_config

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "pipeline_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "pipeline": {
                            "default": {
                                "provider": "openai",
                                "model": "gpt-4o",
                                "backend_mode": "openai_api",
                                "auth_mode": "api_key_env",
                                "output_policy": "redact_all",
                            },
                            "roles": {"dev": {"model": "gpt-5.4-codex"}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            resolved = resolve_role_config("dev", load_pipeline_config(str(config_path)))

        self.assertEqual(resolved["output_policy"], "redact_all")

    def test_contradictory_provider_model_backend_auth_fails_closed(self):
        from pipeline_config import validate_invocation_routing, validate_pipeline_config

        self.assertTrue(
            validate_invocation_routing(
                provider="openai",
                model="claude-sonnet-4-6",
                backend_mode="codex_cli",
                auth_mode="api_key_env",
            )
        )
        errors = validate_pipeline_config(
            {
                "default": {
                    "provider": "openai",
                    "model": "claude-sonnet-4-6",
                    "backend_mode": "claude_cli",
                    "auth_mode": "cli_auth",
                }
            }
        )
        self.assertTrue(any("conflicts" in error or "belongs" in error for error in errors))

    def test_prebuilt_contradictory_request_is_rejected_before_launch(self):
        from ai_invocation import AIInvocationRequest
        from ai_lifecycle import AILifecycleManager

        request = AIInvocationRequest(
            role="dev",
            provider="anthropic",
            model="gpt-4o",
            backend_mode="codex_cli",
            auth_mode="cli_auth",
            cwd=tempfile.gettempdir(),
            prompt="must never launch",
        )
        manager = AILifecycleManager()
        with patch("ai_lifecycle.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "Invalid AI invocation request"):
                manager.create_session(
                    role="dev",
                    prompt="ignored",
                    context={},
                    project_id="test-proj",
                    workspace=tempfile.gettempdir(),
                    invocation_request=request,
                )
        popen.assert_not_called()

    def test_unresolved_request_does_not_fall_back_to_fixture(self):
        from ai_invocation import AIInvocationRequest

        unresolved = AIInvocationRequest(
            role="dev",
            provider="",
            prompt="must never become a fixture invocation",
        )
        with self.assertRaisesRegex(ValueError, "routing is unresolved"):
            unresolved.resolved_backend()

        explicit_fixture = AIInvocationRequest(
            role="test",
            provider="fixture",
            prompt="explicit fixture invocation",
        )
        self.assertEqual(explicit_fixture.resolved_backend(), "fixture")

    def test_persisted_evidence_drops_raw_refs_and_error_text(self):
        from ai_invocation import AIInvocationRequest, AIInvocationResult
        from ai_lifecycle import AILifecycleManager

        request = AIInvocationRequest(
            role="dev",
            provider="openai",
            model="gpt-4o",
            backend_mode="openai_api",
            auth_mode="api_key_env",
            cwd=tempfile.gettempdir(),
            prompt="private raw prompt",
            metadata={
                "evidence_refs": [
                    "trace:graph-1",
                    "prompt:private-raw-prompt",
                    "credential:secret-value",
                    "trace:sk-secretcredential",
                    "not free form evidence",
                ]
            },
        )
        result = AIInvocationResult(
            request=request,
            status="failed",
            error="raw provider output with sk-secretcredential",
            returncode=1,
            raw_output_stored=False,
        )

        evidence = AILifecycleManager.invocation_result_evidence(result)
        serialized = json.dumps(evidence, sort_keys=True)

        self.assertEqual(evidence["evidence_refs"], ["trace:graph-1"])
        self.assertEqual(evidence["error"], "")
        self.assertTrue(evidence["error_present"])
        self.assertTrue(evidence["error_sha256"].startswith("sha256:"))
        self.assertFalse(evidence["raw_error_stored"])
        self.assertFalse(evidence["raw_output_stored"])
        self.assertNotIn("private raw prompt", serialized)
        self.assertNotIn("secretcredential", serialized)

    def test_api_backend_rejects_unknown_or_model_mismatched_provider(self):
        from backends import run_via_api

        with self.assertRaisesRegex(ValueError, "explicit openai or anthropic"):
            run_via_api(
                {"role": "dev"},
                provider_override="provider-from-backend-name",
                model_override="gpt-4o",
                prompt_override="do not invoke",
            )
        with self.assertRaisesRegex(ValueError, "Invalid API invocation routing"):
            run_via_api(
                {"role": "dev"},
                provider_override="anthropic",
                model_override="gpt-4o",
                prompt_override="do not invoke",
            )

    def test_claude_command_for_anthropic(self):
        from ai_lifecycle import AILifecycleManager

        cmd = AILifecycleManager._build_claude_command(
            role="coordinator",
            model="claude-sonnet-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="simple coordinator task",
        )

        self.assertEqual(cmd[0], "claude")
        self.assertIn("--system-prompt-file", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIn("C:\\repo", cmd)
        self.assertIn("--max-turns", cmd)
        self.assertIn("1", cmd)

    def test_claude_command_sets_role_turn_caps(self):
        from ai_lifecycle import AILifecycleManager

        dev_cmd = AILifecycleManager._build_claude_command(
            role="dev",
            model="claude-sonnet-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="small dev task",
        )
        tester_cmd = AILifecycleManager._build_claude_command(
            role="tester",
            model="claude-sonnet-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="run tests",
        )
        gatekeeper_cmd = AILifecycleManager._build_claude_command(
            role="gatekeeper",
            model="claude-sonnet-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="review merge readiness",
        )

        self.assertEqual(dev_cmd[dev_cmd.index("--max-turns") + 1], "40")
        self.assertNotIn("--max-turns", tester_cmd)
        self.assertEqual(gatekeeper_cmd[gatekeeper_cmd.index("--max-turns") + 1], "20")

        qa_cmd = AILifecycleManager._build_claude_command(
            role="qa",
            model="claude-opus-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="qa review",
        )
        self.assertEqual(qa_cmd[qa_cmd.index("--max-turns") + 1], "40")

    def test_claude_command_raises_dev_turn_cap_for_heavy_workflow_task(self):
        from ai_lifecycle import AILifecycleManager

        heavy_context = {
            "target_files": [f"agent/file_{i}.py" for i in range(10)],
            "requirements": [f"R{i}" for i in range(7)],
            "replay_source": "observer-host-governance-fresh-lane-b-rebuild",
        }

        dev_cmd = AILifecycleManager._build_claude_command(
            role="dev",
            model="claude-opus-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context=heavy_context,
            prompt="x" * 6000,
        )

        self.assertEqual(dev_cmd[dev_cmd.index("--max-turns") + 1], "60")

    def test_pm_role_turn_cap_is_60(self):
        from ai_lifecycle import AILifecycleManager, _CLAUDE_ROLE_TURN_CAPS

        self.assertEqual(_CLAUDE_ROLE_TURN_CAPS["pm"], "60")

        pm_cmd = AILifecycleManager._build_claude_command(
            role="pm",
            model="claude-sonnet-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="analyze requirements",
        )
        self.assertEqual(pm_cmd[pm_cmd.index("--max-turns") + 1], "60")

    def test_codex_command_for_openai(self):
        from ai_lifecycle import AILifecycleManager

        with patch.dict(os.environ, {"CODEX_DANGEROUS": "1"}, clear=False):
            cmd = AILifecycleManager._build_codex_command(
                model="gpt-5.4-codex",
                cwd="C:\\repo",
            )

        self.assertEqual(cmd[0], "codex.cmd" if os.name == "nt" else "codex")
        self.assertEqual(cmd[1], "exec")
        self.assertIn("--model", cmd)
        self.assertIn("gpt-5.4-codex", cmd)
        self.assertIn("-C", cmd)
        self.assertIn("C:\\repo", cmd)

    def test_codex_prompt_contains_system_and_task(self):
        from ai_lifecycle import AILifecycleManager

        prompt = AILifecycleManager._compose_codex_prompt("SYSTEM", "TASK")
        self.assertIn("SYSTEM PROMPT START", prompt)
        self.assertIn("SYSTEM", prompt)
        self.assertIn("TASK PROMPT START", prompt)
        self.assertIn("TASK", prompt)


class TestB14StdinPromptPassedToCommunicate(unittest.TestCase):
    """B14: stdin_prompt must be passed to the CLI process."""

    @patch("ai_lifecycle.subprocess.Popen")
    def test_process_stdin_receives_prompt(self, mock_popen):
        from ai_lifecycle import AILifecycleManager

        class CaptureStdin:
            def __init__(self):
                self.value = ""

            def write(self, value):
                self.value += value

            def close(self):
                pass

        class FakeProc:
            def __init__(self):
                self.pid = 999
                self.returncode = 0
                self.stdin = CaptureStdin()
                self.stdout = io.StringIO('{"result":"ok"}\n')
                self.stderr = io.StringIO("")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        fake_proc = FakeProc()
        mock_popen.return_value = fake_proc

        mgr = AILifecycleManager()
        session = mgr.create_session(
            role="pm",
            prompt="Analyze this requirement",
            context={},
            project_id="test-proj",
            workspace=tempfile.gettempdir(),
        )
        mgr.wait_for_output(session.session_id)

        self.assertIn("Analyze this requirement", fake_proc.stdin.value)

    @patch("ai_lifecycle.subprocess.Popen")
    def test_cli_and_api_modes_share_sanitized_route_bound_result_schema(self, mock_popen):
        from ai_lifecycle import AILifecycleManager

        class CaptureStdin:
            def write(self, value):
                self.value = value

            def close(self):
                pass

        class FakeProc:
            pid = 1001
            returncode = 0
            stdin = CaptureStdin()
            stdout = io.StringIO("private provider output\n")
            stderr = io.StringIO("")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        mock_popen.return_value = FakeProc()
        context = {
            "task_id": "task-route-1",
            "backlog_id": "AC-ROUTE-1",
            "runtime_context_id": "mfrctx-1",
            "route_context_hash": "sha256:route",
            "prompt_contract_id": "rprompt-1",
            "prompt_contract_hash": "sha256:prompt",
            "route_token_ref": "rtok-1",
            "evidence_refs": ["trace:graph-1"],
        }

        with tempfile.TemporaryDirectory() as workspace:
            manager = AILifecycleManager()
            with patch.object(
                AILifecycleManager,
                "_resolve_invocation_routing",
                return_value={
                    "provider": "openai",
                    "model": "gpt-5.4-codex",
                    "backend_mode": "codex_cli",
                    "auth_mode": "cli_auth",
                    "output_policy": "hash_and_summary_only",
                },
            ):
                cli_session = manager.create_session(
                    role="dev",
                    prompt="private cli prompt",
                    context=context,
                    project_id="aming-claw",
                    workspace=workspace,
                )
                cli_result = manager.wait_for_output(cli_session.session_id)

            with patch.object(
                AILifecycleManager,
                "_resolve_invocation_routing",
                return_value={
                    "provider": "openai",
                    "model": "gpt-4o",
                    "backend_mode": "openai_api",
                    "auth_mode": "api_key_env",
                    "output_policy": "hash_and_summary_only",
                },
            ), patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
                api_session = manager.create_session(
                    role="dev",
                    prompt="private api prompt",
                    context=context,
                    project_id="aming-claw",
                    workspace=workspace,
                )
                api_result = manager.wait_for_output(api_session.session_id)

            cli_evidence = cli_result["ai_invocation"]
            api_evidence = api_result["ai_invocation"]
            self.assertEqual(set(cli_evidence), set(api_evidence))
            self.assertEqual(cli_evidence["schema_version"], "ai_invocation_result.v1")
            self.assertEqual(api_evidence["auth_status"], "missing_api_key")
            self.assertEqual(
                cli_evidence["route_prompt_contract"]["route_context_hash"],
                "sha256:route",
            )
            self.assertEqual(
                cli_evidence["route_prompt_contract"]["prompt_contract_id"],
                "rprompt-1",
            )
            self.assertIn("runtime_context:mfrctx-1", cli_evidence["evidence_refs"])
            self.assertFalse(cli_evidence["raw_output_stored"])
            self.assertTrue(cli_evidence["no_raw_prompt_output"])

            persisted = "\n".join(
                Path(path).read_text(encoding="utf-8")
                for path in (
                    cli_session.input_path,
                    cli_session.output_path,
                    api_session.input_path,
                    api_session.output_path,
                )
            )
            self.assertNotIn("private cli prompt", persisted)
            self.assertNotIn("private api prompt", persisted)
            self.assertNotIn("private provider output", persisted)
            json.loads(Path(cli_session.output_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
