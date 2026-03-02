"""Tests for workspace.py, workspace_registry.py - workspace management."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from workspace import (  # noqa: E402
    clear_thread_workspace,
    clear_workspace_override,
    get_thread_workspace,
    get_workspace_override,
    resolve_active_workspace,
    resolve_workspace_from_env,
    set_thread_workspace,
    set_workspace_override,
    thread_workspace_context,
)
from workspace_registry import (  # noqa: E402
    add_workspace,
    find_workspace_by_label,
    find_workspace_by_path,
    get_default_workspace,
    get_workspace,
    is_blocked_workspace,
    list_workspaces,
    remove_workspace,
    resolve_workspace_for_task,
    set_default_workspace,
    update_workspace,
)


class TestThreadWorkspace(unittest.TestCase):
    def setUp(self):
        clear_thread_workspace()

    def tearDown(self):
        clear_thread_workspace()

    def test_default_none(self):
        self.assertIsNone(get_thread_workspace())

    def test_set_and_get(self):
        ws = Path("/tmp/test_ws")
        set_thread_workspace(ws)
        self.assertEqual(get_thread_workspace(), ws)

    def test_clear(self):
        set_thread_workspace(Path("/tmp/ws"))
        clear_thread_workspace()
        self.assertIsNone(get_thread_workspace())

    def test_context_manager(self):
        ws = Path("/tmp/ctx_ws")
        self.assertIsNone(get_thread_workspace())
        with thread_workspace_context(ws):
            self.assertEqual(get_thread_workspace(), ws)
        self.assertIsNone(get_thread_workspace())

    def test_nested_context(self):
        ws1 = Path("/tmp/ws1")
        ws2 = Path("/tmp/ws2")
        with thread_workspace_context(ws1):
            self.assertEqual(get_thread_workspace(), ws1)
            with thread_workspace_context(ws2):
                self.assertEqual(get_thread_workspace(), ws2)
            self.assertEqual(get_thread_workspace(), ws1)


class TestWorkspaceOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_none(self):
        self.assertIsNone(get_workspace_override())

    def test_set_and_get(self):
        ws = Path(self.tmp.name) / "myproject"
        ws.mkdir()
        set_workspace_override(ws, changed_by=123)
        loaded = get_workspace_override()
        self.assertEqual(str(loaded), str(ws))

    def test_clear(self):
        set_workspace_override(Path("/tmp/ws"), changed_by=123)
        clear_workspace_override(changed_by=123)
        self.assertIsNone(get_workspace_override())


class TestResolveActiveWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ.pop("CODEX_WORKSPACE", None)
        clear_thread_workspace()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CODEX_WORKSPACE", None)
        clear_thread_workspace()
        self.tmp.cleanup()

    def test_thread_local_priority(self):
        ws = Path(self.tmp.name) / "thread_ws"
        ws.mkdir()
        set_thread_workspace(ws)
        self.assertEqual(resolve_active_workspace(), ws)

    def test_env_fallback(self):
        ws = Path(self.tmp.name) / "env_ws"
        ws.mkdir()
        os.environ["CODEX_WORKSPACE"] = str(ws)
        self.assertEqual(resolve_active_workspace(), ws)

    def test_cwd_default(self):
        # Without any overrides, should return cwd
        result = resolve_active_workspace()
        self.assertIsInstance(result, Path)


class TestResolveWorkspaceFromEnv(unittest.TestCase):
    def test_env_set(self):
        os.environ["CODEX_WORKSPACE"] = "/custom/path"
        self.assertEqual(resolve_workspace_from_env(), Path("/custom/path"))
        os.environ.pop("CODEX_WORKSPACE")

    def test_env_empty(self):
        os.environ.pop("CODEX_WORKSPACE", None)
        self.assertEqual(resolve_workspace_from_env(), Path.cwd())


class TestIsBlockedWorkspace(unittest.TestCase):
    def test_ssh_blocked(self):
        self.assertTrue(is_blocked_workspace(Path("/home/user/.ssh")))

    def test_aws_blocked(self):
        self.assertTrue(is_blocked_workspace(Path("/home/user/.aws")))

    def test_normal_allowed(self):
        self.assertFalse(is_blocked_workspace(Path("/home/user/projects")))


class TestWorkspaceRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create workspace directories
        self.ws1_path = Path(self.tmp.name) / "project-a"
        self.ws1_path.mkdir()
        self.ws2_path = Path(self.tmp.name) / "project-b"
        self.ws2_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_list_empty(self):
        self.assertEqual(list_workspaces(), [])

    def test_add_workspace(self):
        ws = add_workspace(self.ws1_path, label="proj-a")
        self.assertEqual(ws["label"], "proj-a")
        self.assertTrue(ws["id"].startswith("ws-"))
        self.assertTrue(ws["active"])

    def test_add_duplicate_raises(self):
        add_workspace(self.ws1_path)
        with self.assertRaises(ValueError):
            add_workspace(self.ws1_path)

    def test_add_sensitive_raises(self):
        ssh_dir = Path(self.tmp.name) / ".ssh"
        ssh_dir.mkdir()
        with self.assertRaises(ValueError):
            add_workspace(ssh_dir)

    def test_add_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            add_workspace(Path(self.tmp.name) / "nonexistent")

    def test_list_workspaces(self):
        add_workspace(self.ws1_path, label="a")
        add_workspace(self.ws2_path, label="b")
        ws_list = list_workspaces()
        self.assertEqual(len(ws_list), 2)

    def test_get_workspace(self):
        ws = add_workspace(self.ws1_path)
        found = get_workspace(ws["id"])
        self.assertEqual(found["id"], ws["id"])

    def test_find_by_label(self):
        add_workspace(self.ws1_path, label="my-label")
        found = find_workspace_by_label("my-label")
        self.assertIsNotNone(found)
        self.assertEqual(found["label"], "my-label")

    def test_find_by_path(self):
        add_workspace(self.ws1_path)
        found = find_workspace_by_path(self.ws1_path)
        self.assertIsNotNone(found)

    def test_remove_workspace(self):
        ws = add_workspace(self.ws1_path)
        self.assertTrue(remove_workspace(ws["id"]))
        self.assertEqual(list_workspaces(), [])
        # Remove nonexistent
        self.assertFalse(remove_workspace("ws-nonexistent"))

    def test_update_workspace(self):
        ws = add_workspace(self.ws1_path, label="old")
        updated = update_workspace(ws["id"], label="new")
        self.assertEqual(updated["label"], "new")

    def test_default_workspace(self):
        ws1 = add_workspace(self.ws1_path, is_default=True)
        add_workspace(self.ws2_path)
        default = get_default_workspace()
        self.assertEqual(default["id"], ws1["id"])

    def test_set_default(self):
        add_workspace(self.ws1_path, is_default=True)
        ws2 = add_workspace(self.ws2_path)
        set_default_workspace(ws2["id"])
        default = get_default_workspace()
        self.assertEqual(default["id"], ws2["id"])


class TestResolveWorkspaceForTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "project"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_explicit_id(self):
        ws = add_workspace(self.ws_path, label="test")
        task = {"target_workspace_id": ws["id"], "text": "test"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["id"], ws["id"])

    def test_explicit_label(self):
        add_workspace(self.ws_path, label="my-proj")
        task = {"target_workspace": "my-proj", "text": "test"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["label"], "my-proj")

    def test_at_prefix(self):
        add_workspace(self.ws_path, label="demo")
        task = {"text": "@workspace:demo 修复bug"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["label"], "demo")

    def test_default_fallback(self):
        ws = add_workspace(self.ws_path, is_default=True)
        task = {"text": "普通任务"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["id"], ws["id"])


class TestLooksLikePath(unittest.TestCase):
    """Tests for bot_commands._looks_like_path helper."""

    def setUp(self):
        from bot_commands import _looks_like_path
        self._looks_like_path = _looks_like_path

    def test_windows_absolute(self):
        self.assertTrue(self._looks_like_path("C:\\Users\\me\\project"))
        self.assertTrue(self._looks_like_path("D:/repos/foo"))

    def test_unix_absolute(self):
        self.assertTrue(self._looks_like_path("/home/user/project"))
        self.assertTrue(self._looks_like_path("/tmp/test"))

    def test_relative_with_separators(self):
        self.assertTrue(self._looks_like_path("./myproject"))
        self.assertTrue(self._looks_like_path("dir/subdir"))
        self.assertTrue(self._looks_like_path("dir\\subdir"))

    def test_tilde_path(self):
        self.assertTrue(self._looks_like_path("~/projects/foo"))

    def test_keyword_not_path(self):
        self.assertFalse(self._looks_like_path("toolbox"))
        self.assertFalse(self._looks_like_path("my-project"))
        self.assertFalse(self._looks_like_path("frontend"))
        self.assertFalse(self._looks_like_path("aming_claw"))

    def test_empty(self):
        self.assertFalse(self._looks_like_path(""))
        self.assertFalse(self._looks_like_path("   "))


class TestFuzzyWorkspaceAddFlow(unittest.TestCase):
    """Tests for fuzzy workspace search (find_git_workspace_candidates)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create mock git repos
        for name in ["my-toolbox", "toolbox-utils", "frontend-app", "backend-api"]:
            d = self.root / name
            d.mkdir()
            (d / ".git").mkdir()
        # Non-git dir (should not match)
        nogit = self.root / "toolbox-docs"
        nogit.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("WORKSPACE_SEARCH_ROOTS", None)
        self.tmp.cleanup()

    def test_fuzzy_match_keyword(self):
        from bot_commands import find_git_workspace_candidates
        os.environ["WORKSPACE_SEARCH_ROOTS"] = str(self.root)
        results = find_git_workspace_candidates("toolbox")
        names = [p.name for p in results]
        self.assertIn("my-toolbox", names)
        self.assertIn("toolbox-utils", names)
        # toolbox-docs has no .git, should not appear
        self.assertNotIn("toolbox-docs", names)

    def test_fuzzy_no_match(self):
        from bot_commands import find_git_workspace_candidates
        os.environ["WORKSPACE_SEARCH_ROOTS"] = str(self.root)
        results = find_git_workspace_candidates("nonexistent-xyz")
        self.assertEqual(results, [])

    def test_fuzzy_single_match(self):
        from bot_commands import find_git_workspace_candidates
        os.environ["WORKSPACE_SEARCH_ROOTS"] = str(self.root)
        results = find_git_workspace_candidates("frontend")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "frontend-app")


if __name__ == "__main__":
    unittest.main()
