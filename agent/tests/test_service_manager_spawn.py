"""Tests for executor spawn command — script-path form (NOT module form).

Embedded Python runtime has restrictive python312._pth that doesn't include
project root. Module form (`-m agent.executor_worker`) fails with
ModuleNotFoundError before executor_worker.py can run. Script-path form lets
executor_worker.py's own _proj_root sys.path bootstrap handle the agent.*
imports it needs internally.
"""
import subprocess
import sys
from pathlib import Path

from agent.service_manager import _default_executor_cmd, _repo_root


def test_spawn_command_uses_script_path_form():
    """AC1: cmd[1] points at executor_worker.py path (NOT -m module form).

    Embedded Python _pth restriction breaks module form — script-path required.
    """
    cmd = _default_executor_cmd("test-proj", "http://localhost:40000", "/tmp")
    assert len(cmd) >= 2
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("executor_worker.py"), (
        f"Expected script-path form (executor_worker.py), got {cmd[1]}"
    )
    assert "-m" not in cmd, (
        "Module form (-m) breaks under embedded Python _pth restriction; "
        "script-path form must be used (see executor_worker.py _proj_root bootstrap)"
    )


def test_executor_worker_has_sys_path_bootstrap():
    """AC2: executor_worker.py adds project root to sys.path at module-load time.

    This bootstrap is what makes script-path form work — it lets the script
    import `from agent.governance.X import Y` regardless of how Python is invoked.
    """
    worker_path = _repo_root() / "agent" / "executor_worker.py"
    src = worker_path.read_text(encoding="utf-8")
    assert "_proj_root" in src and "sys.path.insert" in src, (
        "executor_worker.py must add project root to sys.path before importing "
        "agent.governance.* — see top-of-file _proj_root bootstrap pattern"
    )


def test_spawn_command_workspace_param_present():
    """AC3: cmd includes --workspace argument (regression guard)."""
    cmd = _default_executor_cmd("test-proj", "http://localhost:40000", "/some/workspace")
    assert "--workspace" in cmd
    idx = cmd.index("--workspace")
    assert cmd[idx + 1] == "/some/workspace"


def test_default_spawn_command_actually_boots():
    """The spawn command from _default_executor_cmd must actually run without ModuleNotFoundError."""
    cmd = _default_executor_cmd("test-proj", "http://localhost:40000", "/tmp")
    result = subprocess.run(cmd + ["--help"], capture_output=True, text=True, timeout=15)
    assert "ModuleNotFoundError" not in result.stderr, f"ModuleNotFoundError in stderr:\n{result.stderr}"
    combined = (result.stdout + result.stderr).lower()
    assert "usage:" in combined, f"Expected 'usage:' in output, got:\n{result.stdout}\n{result.stderr}"


def test_executor_can_import_chain_trailer_in_subprocess():
    """A subprocess mirroring executor_worker.py's sys.path bootstrap can import chain_trailer."""
    probe_path = _repo_root() / "agent" / "_probe_chain_trailer.py"
    probe_code = (
        "import sys, os\n"
        "_proj_root = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "if _proj_root not in sys.path:\n"
        "    sys.path.insert(0, _proj_root)\n"
        "from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state\n"
        "print('IMPORTS_OK')\n"
    )
    try:
        probe_path.write_text(probe_code, encoding="utf-8")
        result = subprocess.run([sys.executable, str(probe_path)], capture_output=True, text=True, timeout=15)
        assert "IMPORTS_OK" in result.stdout, f"Expected IMPORTS_OK in stdout, got:\n{result.stdout}"
        assert "ModuleNotFoundError" not in result.stderr, f"ModuleNotFoundError in stderr:\n{result.stderr}"
    finally:
        if probe_path.exists():
            probe_path.unlink()
