"""Focused tests for manager governance process spawning."""

from pathlib import Path


def test_spawn_governance_uses_host_entrypoint_for_bundled_python(monkeypatch):
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

    proc = manager_http_server._spawn_governance_process("abc1234")

    assert proc.pid == 12345
    assert captured["cmd"][0] == "python-test"
    assert Path(captured["cmd"][1]).name == "start_governance.py"
    assert "-m" not in captured["cmd"]
