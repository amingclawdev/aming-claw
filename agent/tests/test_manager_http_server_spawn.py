"""Focused tests for manager governance process spawning."""

from pathlib import Path
import subprocess


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
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path / "shared"))

    proc = manager_http_server._spawn_governance_process("abc1234")

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

    shared = tmp_path / "shared"
    monkeypatch.setattr(manager_http_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager_http_server.sys, "executable", "python-test")
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(shared))

    manager_http_server._spawn_governance_process("abc1234")

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
