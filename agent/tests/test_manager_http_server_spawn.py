"""Focused tests for manager governance process spawning."""

from pathlib import Path
import subprocess

import pytest


def test_spawn_governance_uses_host_entrypoint_for_bundled_python(monkeypatch, tmp_path):
    """Bundled python needs start_governance.py to seed sys.path before import."""
    import agent.manager_http_server as manager_http_server

    captured = {}

    class Proc:
        pid = 12345

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(manager_http_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_http_server.sys, "executable", "python-test")
    (tmp_path / "shared-volume").mkdir()
    monkeypatch.setattr(manager_http_server, "_project_root", lambda: tmp_path)
    identity = manager_http_server.plane_bound_manager_identity(
        "proj", "http://127.0.0.1:40000", str(tmp_path / "shared-volume"),
    )

    proc = manager_http_server._spawn_governance_process(identity, "abc1234")

    assert proc.pid == 12345
    assert captured["cmd"][0] == "python-test"
    assert Path(captured["cmd"][1]).name == "start_governance.py"
    assert "-m" not in captured["cmd"]


def test_spawn_governance_persists_stdout_and_stderr(monkeypatch, tmp_path):
    """Manager redeploy must not leave governance output in unconsumed pipes."""
    import agent.manager_http_server as manager_http_server

    captured = {}

    class Proc:
        pid = 12345

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Proc()

    shared = tmp_path / "shared-volume"
    shared.mkdir()
    monkeypatch.setattr(manager_http_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_http_server.sys, "executable", "python-test")
    monkeypatch.setattr(manager_http_server, "_project_root", lambda: tmp_path)
    identity = manager_http_server.plane_bound_manager_identity(
        "proj", "http://127.0.0.1:40000", str(shared),
    )

    manager_http_server._spawn_governance_process(identity, "abc1234")

    stdout_handle = captured["kwargs"]["stdout"]
    stderr_handle = captured["kwargs"]["stderr"]
    log_dir = shared / "codex-tasks" / "logs"

    assert stdout_handle is not subprocess.PIPE
    assert stderr_handle is not subprocess.PIPE
    assert Path(stdout_handle.name).parent == log_dir
    assert Path(stderr_handle.name).parent == log_dir
    assert Path(stdout_handle.name).name.startswith("governance-redeploy-40000-abc1234-")
    assert Path(stderr_handle.name).name.startswith("governance-redeploy-40000-abc1234-")
    assert Path(stdout_handle.name).suffixes[-2:] == [".out", ".log"]
    assert Path(stderr_handle.name).suffixes[-2:] == [".err", ".log"]
    assert captured["kwargs"]["env"]["GOVERNANCE_STDOUT_LOG"] == stdout_handle.name
    assert captured["kwargs"]["env"]["GOVERNANCE_STDERR_LOG"] == stderr_handle.name
    assert captured["kwargs"]["env"]["PROJECT_ID"] == "proj"
    assert captured["kwargs"]["env"]["GOVERNANCE_URL"] == "http://127.0.0.1:40000"


@pytest.mark.parametrize(
    ("project_id", "governance_url", "expected_port"),
    [
        ("proj", "http://127.0.0.1:40000", "40100"),
        ("aming-claw", "http://127.0.0.1:40008", "40108"),
    ],
)
def test_spawn_environment_cannot_escape_bound_identity(
    monkeypatch, tmp_path, project_id, governance_url, expected_port,
):
    import agent.manager_http_server as manager_http_server

    class Proc:
        pid = 12345

    captured = {}
    monkeypatch.setattr(
        manager_http_server.subprocess, "Popen",
        lambda cmd, **kwargs: captured.update(kwargs) or Proc(),
    )
    monkeypatch.setenv("GOVERNANCE_URL", "http://127.0.0.1:40000")
    monkeypatch.setenv("PROJECT_ID", "ambient-project")
    monkeypatch.setenv("EXECUTOR_PROJECT_ID", "ambient-project")
    monkeypatch.setenv("SHARED_VOLUME_PATH", "/ambient/root")
    monkeypatch.setenv("MANAGER_URL", "http://127.0.0.1:40101")
    monkeypatch.setenv("EXECUTOR_API_PORT", "40100")
    if project_id == "aming-claw":
        storage = tmp_path / "dev-storage"
        root = storage / "runtime"
        root.mkdir(parents=True)
        monkeypatch.setenv("AMING_CLAW_DEV_STORAGE_ROOT", str(storage))
    else:
        root = tmp_path / "shared-volume"
        root.mkdir()
        monkeypatch.setattr(manager_http_server, "_project_root", lambda: tmp_path)
    identity = manager_http_server.plane_bound_manager_identity(project_id, governance_url, str(root))

    manager_http_server._spawn_governance_process(identity, "abc1234")

    env = captured["env"]
    assert env["GOVERNANCE_URL"] == governance_url
    assert env["PROJECT_ID"] == project_id
    assert env["EXECUTOR_PROJECT_ID"] == project_id
    assert env["SHARED_VOLUME_PATH"] == identity["storage_root"]
    assert env["MANAGER_URL"] == identity["manager_url"]
    assert env["EXECUTOR_API_PORT"] == expected_port
