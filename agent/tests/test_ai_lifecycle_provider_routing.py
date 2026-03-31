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
        self.assertEqual(tester_cmd[tester_cmd.index("--max-turns") + 1], "20")
        self.assertEqual(gatekeeper_cmd[gatekeeper_cmd.index("--max-turns") + 1], "20")

        qa_cmd = AILifecycleManager._build_claude_command(
            role="qa",
            model="claude-opus-4-6",
            prompt_file="C:\\temp\\ctx.md",
            cwd="C:\\repo",
            context={},
            prompt="qa review",
        )
        self.assertEqual(qa_cmd[qa_cmd.index("--max-turns") + 1], "20")

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


if __name__ == "__main__":
    unittest.main()
