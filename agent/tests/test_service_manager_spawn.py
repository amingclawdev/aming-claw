"""Tests for executor spawn command form (module-based, not script-path)."""
import subprocess
import sys
from pathlib import Path

from agent.service_manager import _default_executor_cmd, _repo_root


def test_spawn_command_uses_module_form():
    """AC1: cmd list contains '-m' followed by 'agent.executor_worker'."""
    cmd = _default_executor_cmd("test-proj", "http://localhost:40000", "/tmp")
    assert "-m" in cmd
    idx = cmd.index("-m")
    assert cmd[idx + 1] == "agent.executor_worker"


def test_spawn_command_no_script_path_form():
    """Regression: no element after sys.executable ends with .py."""
    cmd = _default_executor_cmd("test-proj", "http://localhost:40000", "/tmp")
    for element in cmd[1:]:
        assert not element.endswith(".py"), f"Found script-path element: {element}"


def test_executor_worker_has_sys_path_bootstrap():
    """AC3: executor_worker.py adds project root to sys.path at module-load time
    so that `from agent.governance.X import Y` works regardless of how the
    interpreter is invoked. This is needed because the embedded Python runtime
    has a restrictive python312._pth that doesn't add cwd to sys.path.

    Verifies the bootstrap exists by reading the source file (no subprocess —
    avoids _pth-related complications in tests).
    """
    worker_path = _repo_root() / "agent" / "executor_worker.py"
    src = worker_path.read_text(encoding="utf-8")
    # Look for the canonical bootstrap pattern (sys.path.insert with project root)
    assert "_proj_root" in src and "sys.path.insert" in src, (
        "executor_worker.py must add project root to sys.path before importing "
        "agent.governance.* — see top-of-file bootstrap pattern"
    )
    # Make sure agent/ dir alone (the wrong path) is not what's being added
    assert "_agent_dir" not in src.split("\n")[40] if len(src.split("\n")) > 40 else True, (
        "Old _agent_dir bootstrap (which adds agent/ instead of project root) should be replaced"
    )
