import inspect
import subprocess


def test_windows_process_tree_kill_uses_taskkill_tree(monkeypatch):
    from agent import ai_lifecycle

    captured = {}

    class Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(ai_lifecycle.os, "name", "nt", raising=False)
    monkeypatch.setattr(ai_lifecycle.subprocess, "run", fake_run)

    assert ai_lifecycle._kill_process_tree(1234) is True
    assert captured["cmd"] == ["taskkill", "/F", "/T", "/PID", "1234"]
    assert captured["kwargs"]["stdout"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is subprocess.DEVNULL
    assert captured["kwargs"]["timeout"] == 10
    assert captured["kwargs"]["check"] is False


def test_kill_session_delegates_to_process_tree_kill(monkeypatch):
    from agent.ai_lifecycle import AILifecycleManager, AISession
    from agent import ai_lifecycle

    killed_pids = []

    def fake_kill_process_tree(pid):
        killed_pids.append(pid)
        return True

    manager = AILifecycleManager()
    session = AISession(
        session_id="ai-dev-test",
        role="dev",
        pid=4321,
        project_id="aming-claw",
        prompt="",
        context={},
        started_at=0.0,
        timeout_sec=120,
    )
    manager._sessions[session.session_id] = session
    monkeypatch.setattr(ai_lifecycle, "_kill_process_tree", fake_kill_process_tree)

    assert manager.kill_session(session.session_id, "test") is True
    assert killed_pids == [4321]
    assert session.status == "killed"


def test_timeout_path_does_not_call_proc_kill():
    from agent import ai_lifecycle

    source = inspect.getsource(ai_lifecycle.AILifecycleManager.create_session)
    assert "proc.kill()" not in source
    assert "_kill_process_tree(proc.pid or session.pid)" in source


def test_create_session_uses_streaming_reader_watchdog():
    from agent import ai_lifecycle

    source = inspect.getsource(ai_lifecycle.AILifecycleManager.create_session)
    assert "proc.communicate(input=stdin_prompt, timeout=_MAX_TIMEOUT)" not in source
    assert "stdout_thread.start()" in source
    assert "session.last_heartbeat" in source
    assert "no CLI output" in source
