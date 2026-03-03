"""Tests for bot_commands.task_inline_keyboard."""
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from bot_commands import task_inline_keyboard  # noqa: E402


class TestTaskInlineKeyboard(unittest.TestCase):
    """Verify task_inline_keyboard returns correct buttons after creation."""

    def test_no_accept_button(self):
        kb = task_inline_keyboard("T0001")
        all_cb = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertFalse(any(cb.startswith("accept:") for cb in all_cb),
                         "accept button should not be present")

    def test_no_reject_button(self):
        kb = task_inline_keyboard("T0001")
        all_cb = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertFalse(any(cb.startswith("reject:") for cb in all_cb),
                         "reject button should not be present")

    def test_has_status_button(self):
        kb = task_inline_keyboard("T0001")
        all_cb = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any(cb.startswith("status:") for cb in all_cb),
                        "status button should be present")

    def test_has_events_button(self):
        kb = task_inline_keyboard("T0001")
        all_cb = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertTrue(any(cb.startswith("events:") for cb in all_cb),
                        "events button should be present")

    def test_ref_in_callback_data(self):
        kb = task_inline_keyboard("T9999")
        all_cb = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("status:T9999", all_cb)
        self.assertIn("events:T9999", all_cb)


if __name__ == "__main__":
    unittest.main()
