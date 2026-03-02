import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import bot_commands  # noqa: E402


class ScreenshotCommandRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_task_style_screenshot_command_creates_task_instead_of_running_capture(self) -> None:
        with patch.object(bot_commands, "run_screenshot_once") as run_capture, patch.object(
            bot_commands, "send_text"
        ) as send_text:
            ok = bot_commands.handle_command(
                chat_id=123,
                user_id=456,
                text="/screenshot 命令误判修复",
            )
            self.assertTrue(ok)
            run_capture.assert_not_called()
            self.assertTrue(send_text.called)
            msg = send_text.call_args_list[0][0][1]
            self.assertIn("任务创建", msg)


if __name__ == "__main__":
    unittest.main()
