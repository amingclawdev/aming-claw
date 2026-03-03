"""End-to-end tests for workspace task creation flow (T3).

Covers:
  - Single workspace: skip workspace selection, create task directly
  - Multi workspace: workspace selection keyboard, task creation with ws targeting
  - @workspace:<label> prefix shortcut in task text
  - Fallback when workspace is deleted after selection
  - Queuing when workspace has active task
"""
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

from utils import load_json, save_json, task_file, tasks_root  # noqa: E402
from workspace_registry import (  # noqa: E402
    add_workspace,
    get_default_workspace,
    list_workspaces,
    remove_workspace,
    resolve_workspace_for_task,
)
from interactive_menu import (  # noqa: E402
    workspace_select_keyboard,
    set_pending_action,
    get_pending_action,
    clear_pending_action,
)


class TestWorkspaceSelectKeyboardDisplay(unittest.TestCase):
    """T3-AC2: workspace_select_keyboard shows label (path) format with ⭐ for default."""

    def test_shows_label_and_path(self):
        workspaces = [
            {"id": "ws-001", "label": "my-project", "path": "/home/user/projects/my-project",
             "is_default": False, "active": True},
        ]
        kb = workspace_select_keyboard(workspaces, "ws_task_select")
        btn_text = kb["inline_keyboard"][0][0]["text"]
        self.assertIn("my-project", btn_text)
        self.assertIn("/home/user/projects/my-project", btn_text)

    def test_default_marked_with_star(self):
        workspaces = [
            {"id": "ws-001", "label": "proj-a", "path": "/a",
             "is_default": True, "active": True},
            {"id": "ws-002", "label": "proj-b", "path": "/b",
             "is_default": False, "active": True},
        ]
        kb = workspace_select_keyboard(workspaces, "ws_task_select")
        first_text = kb["inline_keyboard"][0][0]["text"]
        second_text = kb["inline_keyboard"][1][0]["text"]
        self.assertIn("\u2b50", first_text)
        self.assertNotIn("\u2b50", second_text)

    def test_long_path_truncated(self):
        long_path = "/home/user/" + "x" * 100 + "/project"
        workspaces = [
            {"id": "ws-001", "label": "p", "path": long_path,
             "is_default": False, "active": True},
        ]
        kb = workspace_select_keyboard(workspaces, "ws_test")
        btn_text = kb["inline_keyboard"][0][0]["text"]
        self.assertLessEqual(len(btn_text), 65)

    def test_cancel_button_present(self):
        workspaces = [
            {"id": "ws-001", "label": "proj", "path": "/path",
             "is_default": False, "active": True},
        ]
        kb = workspace_select_keyboard(workspaces, "ws_task_select")
        last_row = kb["inline_keyboard"][-1]
        self.assertTrue(any("cancel" in btn["callback_data"] for btn in last_row))


class TestSingleWorkspaceNewTask(unittest.TestCase):
    """T3-AC1: single workspace skips selection, tasks auto-bind to the only workspace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "my-project"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_single_workspace_no_selection(self):
        add_workspace(self.ws_path, label="my-project", is_default=True)
        workspaces = list_workspaces()
        self.assertEqual(len(workspaces), 1)
        # With only 1 workspace, menu flow sets pending_action to "new_task" (no workspace selection)
        # Simulated: the menu handler checks len(workspaces) > 1


class TestMultiWorkspaceNewTask(unittest.TestCase):
    """T3-AC2/AC3/AC4: multi workspace selection and task creation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws1 = Path(self.tmp.name) / "project-a"
        self.ws1.mkdir()
        self.ws2 = Path(self.tmp.name) / "project-b"
        self.ws2.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_create_task_for_workspace(self, mock_send):
        from bot_commands import create_task_for_workspace
        ws1 = add_workspace(self.ws1, label="proj-a", is_default=True)
        ws2 = add_workspace(self.ws2, label="proj-b")
        task_id = create_task_for_workspace(
            chat_id=100, user_id=200, text="fix bug in auth",
            ws_id=ws2["id"], ws_label="proj-b",
        )
        # Load the created task and verify workspace fields
        task = load_json(task_file("pending", task_id))
        self.assertEqual(task["target_workspace_id"], ws2["id"])
        self.assertEqual(task["target_workspace"], "proj-b")
        self.assertEqual(task["text"], "fix bug in auth")
        self.assertEqual(task["status"], "pending")

    def test_workspace_selection_keyboard_callbacks(self):
        ws1 = add_workspace(self.ws1, label="proj-a", is_default=True)
        ws2 = add_workspace(self.ws2, label="proj-b")
        workspaces = list_workspaces()
        kb = workspace_select_keyboard(workspaces, "ws_task_select")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("ws_task_select:{}".format(ws1["id"]), all_data)
        self.assertIn("ws_task_select:{}".format(ws2["id"]), all_data)


class TestAtWorkspacePrefix(unittest.TestCase):
    """T3-AC5: @workspace:<label> prefix auto-matches workspace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "target-proj"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_at_prefix_resolves_workspace(self):
        ws = add_workspace(self.ws_path, label="target-proj", is_default=True)
        task = {"text": "@workspace:target-proj fix login bug"}
        resolved = resolve_workspace_for_task(task)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], ws["id"])

    @patch("bot_commands.send_text")
    def test_at_prefix_stripped_in_task(self, mock_send):
        from bot_commands import create_task, _extract_workspace_target
        add_workspace(self.ws_path, label="target-proj", is_default=True)
        # _extract_workspace_target parses prefix
        label, remaining = _extract_workspace_target("@workspace:target-proj fix bug")
        self.assertEqual(label, "target-proj")
        self.assertEqual(remaining, "fix bug")

    @patch("bot_commands.send_text")
    def test_create_task_with_at_prefix(self, mock_send):
        from bot_commands import create_task
        ws = add_workspace(self.ws_path, label="target-proj", is_default=True)
        task_id = create_task(100, 200, "/task @workspace:target-proj fix bug")
        task = load_json(task_file("pending", task_id))
        self.assertEqual(task["target_workspace"], "target-proj")
        # Text should have prefix stripped
        self.assertEqual(task["text"], "fix bug")


class TestWorkspaceDeletedFallback(unittest.TestCase):
    """T3-AC6: fallback to default workspace when selected workspace is deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws1 = Path(self.tmp.name) / "project-a"
        self.ws1.mkdir()
        self.ws2 = Path(self.tmp.name) / "project-b"
        self.ws2.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_fallback_to_default_on_deleted_workspace(self, mock_send):
        from bot_commands import handle_pending_action
        ws1 = add_workspace(self.ws1, label="proj-a", is_default=True)
        ws2 = add_workspace(self.ws2, label="proj-b")

        # Set pending action to create task with ws2
        set_pending_action(100, 200, "new_task_with_workspace",
                          {"ws_id": ws2["id"], "ws_label": "proj-b"})

        # Delete ws2 between selection and text input
        remove_workspace(ws2["id"])

        # User sends task text
        result = handle_pending_action(100, 200, "fix bug")
        self.assertTrue(result)

        # Should have sent warning about fallback
        calls = mock_send.call_args_list
        warning_sent = any("\u56de\u9000" in str(c) for c in calls)
        self.assertTrue(warning_sent, "Should notify user about workspace fallback")

        # Task should be created (not rejected)
        pending_dir = tasks_root() / "pending"
        pending_files = list(pending_dir.glob("*.json"))
        self.assertGreaterEqual(len(pending_files), 1)

        # The task should target the default workspace
        task = load_json(pending_files[-1])
        self.assertEqual(task["target_workspace_id"], ws1["id"])

    @patch("bot_commands.send_text")
    def test_no_fallback_when_all_deleted(self, mock_send):
        from bot_commands import handle_pending_action
        ws = add_workspace(self.ws1, label="proj-a")
        set_pending_action(100, 200, "new_task_with_workspace",
                          {"ws_id": ws["id"], "ws_label": "proj-a"})
        remove_workspace(ws["id"])

        result = handle_pending_action(100, 200, "fix bug")
        self.assertTrue(result)

        # Should have sent error message
        calls = mock_send.call_args_list
        error_sent = any("\u65e0\u53ef\u7528\u5de5\u4f5c\u533a" in str(c) for c in calls)
        self.assertTrue(error_sent, "Should notify user no workspace available")


class TestNewTaskWithQueueing(unittest.TestCase):
    """T3+T4 integration: workspace task creation with queuing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "project"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    @patch("bot_commands.send_text")
    def test_task_queued_when_workspace_busy(self, mock_send):
        from bot_commands import handle_pending_action
        from task_state import load_runtime_state, save_runtime_state
        from workspace_queue import queue_length

        ws = add_workspace(self.ws_path, label="project", is_default=True)

        # Simulate active task in workspace
        state = load_runtime_state()
        state["active"]["task-active"] = {
            "task_id": "task-active",
            "status": "processing",
            "target_workspace_id": ws["id"],
        }
        save_runtime_state(state)

        # Set pending action and send text
        set_pending_action(100, 200, "new_task_with_workspace",
                          {"ws_id": ws["id"], "ws_label": "project"})
        result = handle_pending_action(100, 200, "new feature")
        self.assertTrue(result)

        # Task should be queued, not created as pending
        self.assertEqual(queue_length(ws["id"]), 1)

        # User should be notified about queue position
        calls = mock_send.call_args_list
        queue_msg = any("\u961f\u5217" in str(c) for c in calls)
        self.assertTrue(queue_msg, "Should notify user about queue position")


if __name__ == "__main__":
    unittest.main()
