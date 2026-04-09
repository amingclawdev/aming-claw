import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestAILifecycleProviderRouting(unittest.TestCase):
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
        self.assertEqual(tester_cmd[tester_cmd.index("--max-turns") + 1], "40")
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
    """B14: proc.communicate() must pass input=stdin_prompt to Claude CLI."""

    @patch("ai_lifecycle.subprocess.Popen")
    def test_communicate_receives_stdin_prompt(self, mock_popen):
        from ai_lifecycle import AILifecycleManager

        fake_proc = mock_popen.return_value
        fake_proc.pid = 999
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ('{"result":"ok"}', "")

        mgr = AILifecycleManager()
        session = mgr.create_session(
            role="pm",
            prompt="Analyze this requirement",
            context={},
            project_id="test-proj",
            workspace=tempfile.gettempdir(),
        )
        mgr.wait_for_output(session.session_id)

        # The key assertion: communicate was called WITH input= containing the prompt
        fake_proc.communicate.assert_called_once()
        call_kwargs = fake_proc.communicate.call_args
        # input= can be positional or keyword
        input_val = call_kwargs.kwargs.get("input") or (call_kwargs.args[0] if call_kwargs.args else None)
        self.assertIsNotNone(input_val, "communicate() must be called with input= parameter")
        self.assertIn("Analyze this requirement", input_val)


if __name__ == "__main__":
    unittest.main()
