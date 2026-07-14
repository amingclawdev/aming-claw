from __future__ import annotations

import ast
import json
import os
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _fake_cli(tmp_path: Path, name: str) -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def _timeout_process_tree_cli(tmp_path: Path) -> Path:
    child_code = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "heartbeat = Path(os.environ['PROFILE_AUTH_TEST_HEARTBEAT'])\n"
        "deadline = time.monotonic() + 3.0\n"
        "while time.monotonic() < deadline:\n"
        "    heartbeat.write_text(str(time.monotonic()), encoding='utf-8')\n"
        "    time.sleep(0.01)\n"
    )
    executable = tmp_path / "codex-timeout"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child_code = {!r}\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code])\n"
        "Path(os.environ['PROFILE_AUTH_TEST_CHILD_PID']).write_text(\n"
        "    str(child.pid), encoding='utf-8'\n"
        ")\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True:\n"
        "    time.sleep(1)\n".format(child_code),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "expected"),
    [
        ('{"authenticated": true}', "", 0, "ready"),
        ('{"authenticated": false}', "", 1, "login_required"),
        ("", "session expired", 1, "expired"),
        ("", "access revoked", 1, "revoked"),
        ("", "account blocked", 1, "blocked"),
        ("", "unrecognized failure", 1, "error"),
    ],
)
def test_auth_state_classification_is_bounded(
    stdout, stderr, returncode, expected
):
    from cli_agent_service.auth import classify_auth_state

    state, reason = classify_auth_state(
        "codex", returncode, stdout=stdout, stderr=stderr
    )

    assert state == expected
    assert reason.startswith("codex_")
    if stdout:
        assert stdout not in reason
    if stderr:
        assert stderr not in reason


def test_timeout_precedes_partial_authenticated_json():
    from cli_agent_service.auth import classify_auth_state

    state, reason = classify_auth_state(
        "codex",
        None,
        stdout='{"authenticated": true}',
        timed_out=True,
    )

    assert state == "blocked"
    assert reason == "codex_status_probe_timed_out"


def test_prepare_codex_login_creates_clean_private_home_and_copy_safe_actions(
    tmp_path,
):
    from cli_agent_service.auth import ProfileAuthController

    executable = _fake_cli(tmp_path, "codex")
    controller = ProfileAuthController(
        tmp_path / "profiles", codex_executable=str(executable)
    )

    result = controller.prepare_login("profile-codex-a", "codex")

    home = Path(result.profile_home)
    assert result.state == "login_in_progress"
    assert result.ok is True
    assert home.is_dir()
    assert not any(home.iterdir())
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert result.environment == {"CODEX_HOME": str(home)}
    assert "CODEX_HOME=" in result.copy_command
    assert "login --device-auth" in result.copy_command
    assert str(executable) in result.copy_command
    assert [action["action"] for action in result.actions] == [
        "open_terminal",
        "copy_command",
    ]
    assert all(action["user_triggered"] for action in result.actions)
    assert all(action["auto_execute"] is False for action in result.actions)
    assert "token" not in result.copy_command.casefold()


def test_codex_auth_operations_share_desktop_bundle_discovery(
    tmp_path, monkeypatch
):
    from cli_agent_service.adapters import codex_cli
    from cli_agent_service.auth import ProfileAuthController

    executable = _fake_cli(tmp_path, "desktop-codex")
    calls = []

    def desktop_codex_ready_runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, "Logged in", "")

    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(codex_cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        codex_cli,
        "MACOS_CODEX_APP_BUNDLE_EXECUTABLES",
        (str(executable),),
    )
    controller = ProfileAuthController(
        tmp_path / "profiles",
        runner=desktop_codex_ready_runner,
    )

    prepared = controller.prepare_login("profile-codex-a", "codex")
    status = controller.auth_status("profile-codex-a", "codex")
    activated = controller.activate("profile-codex-a", "codex")

    assert prepared.state == "login_in_progress"
    assert str(executable) in prepared.copy_command
    assert status.state == "ready"
    assert activated.state == "ready"
    assert activated.activated is True
    assert [command[0] for command, _kwargs in calls] == [
        str(executable),
        str(executable),
    ]


def test_codex_status_and_activation_are_profile_scoped_and_redacted(tmp_path):
    from cli_agent_service.auth import ProfileAuthController

    executable = _fake_cli(tmp_path, "codex")
    calls = []

    def codex_ready_runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            "Logged in using ChatGPT for private@example.com",
            "",
        )

    controller = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=str(executable),
        runner=codex_ready_runner,
    )
    prepared = controller.prepare_login("profile-codex-a", "openai")
    activated = controller.activate("profile-codex-a", "codex")

    assert activated.state == "ready"
    assert activated.activated is True
    assert activated.environment == {"CODEX_HOME": prepared.profile_home}
    assert calls[0][0] == (str(executable), "login", "status")
    assert calls[0][1]["env"]["CODEX_HOME"] == prepared.profile_home
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]
    public = activated.to_public_dict()
    assert "private@example.com" not in json.dumps(public)
    assert public["evidence"]["output_hash"].startswith("sha256:")
    assert public["evidence"]["raw_provider_output_persisted"] is False


def test_unauthenticated_status_returns_provider_specific_login_required(tmp_path):
    from cli_agent_service.auth import ProfileAuthController

    executable = _fake_cli(tmp_path, "codex")

    def codex_login_required_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "Not logged in")

    controller = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=str(executable),
        runner=codex_login_required_runner,
    )
    controller.prepare_login("profile-codex-a", "codex")
    status_result = controller.auth_status("profile-codex-a", "codex")

    assert status_result.state == "login_required"
    assert status_result.evidence["provider"] == "codex"
    assert status_result.evidence["operation"] == "codex_auth_status"
    assert status_result.evidence["status_args"] == ["login", "status"]
    assert "Not logged in" not in json.dumps(status_result.to_public_dict())


def test_activation_fails_closed_on_timed_out_authenticated_output(tmp_path):
    from cli_agent_service.auth import ProfileAuthController

    executable = _fake_cli(tmp_path, "codex")

    def timed_out_authenticated_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output='{"authenticated": true}',
            stderr="",
        )

    controller = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=str(executable),
        runner=timed_out_authenticated_runner,
    )
    controller.prepare_login("profile-codex-a", "codex")

    activated = controller.activate("profile-codex-a", "codex")

    assert activated.state == "blocked"
    assert activated.activated is False
    assert activated.reason_code == "codex_status_probe_timed_out"
    assert activated.evidence["timed_out"] is True


def test_claude_activation_requires_spike_and_never_claims_subscription_isolation(
    tmp_path,
):
    from cli_agent_service.auth import ProfileAuthController

    executable = _fake_cli(tmp_path, "claude")

    def claude_ready_runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, '{"loggedIn": true}', ""
        )

    blocked_controller = ProfileAuthController(
        tmp_path / "blocked-profiles",
        claude_executable=str(executable),
        runner=claude_ready_runner,
    )
    blocked_controller.prepare_login("profile-claude-a", "claude")
    blocked = blocked_controller.activate("profile-claude-a", "anthropic")

    assert blocked.state == "blocked"
    assert blocked.activated is False
    assert blocked.reason_code == "claude_verified_spike_required"
    assert blocked.evidence["subscription_isolation_supported"] is False
    assert blocked.evidence["subscription_isolation_claimed"] is False

    ready_controller = ProfileAuthController(
        tmp_path / "ready-profiles",
        claude_executable=str(executable),
        runner=claude_ready_runner,
        claude_spike_decision="unattended-safe",
    )
    prepared = ready_controller.prepare_login("profile-claude-b", "claude")
    ready = ready_controller.activate("profile-claude-b", "claude")

    assert ready.state == "ready"
    assert ready.activated is True
    assert ready.environment == {"CLAUDE_CONFIG_DIR": prepared.profile_home}
    assert ready.evidence["claude_spike_decision"] == "unattended-safe"
    assert ready.evidence["subscription_isolation_supported"] is False
    assert ready.evidence["subscription_isolation_claimed"] is False


def test_profile_id_cannot_escape_managed_root(tmp_path):
    from cli_agent_service.auth import ProfileAuthController, ProfileAuthError

    controller = ProfileAuthController(tmp_path / "profiles")
    with pytest.raises(ProfileAuthError, match="safe managed profile id"):
        controller.prepare_login("../outside", "codex")
    assert not (tmp_path / "outside").exists()


def test_existing_profile_home_symlink_escape_is_rejected(tmp_path):
    from cli_agent_service.auth import ProfileAuthController, ProfileAuthError

    executable = _fake_cli(tmp_path, "codex")
    calls = []

    def symlink_escape_runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "Logged in", "")

    controller = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=str(executable),
        runner=symlink_escape_runner,
    )
    prepared = controller.prepare_login("profile-codex-a", "codex")
    profile_home = Path(prepared.profile_home)
    profile_home.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    profile_home.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProfileAuthError, match="managed profile path"):
        controller.auth_status("profile-codex-a", "codex")

    assert calls == []
    assert not any(outside.iterdir())


def test_configured_profiles_root_rejects_escaping_ancestor_symlink(tmp_path):
    from cli_agent_service.auth import ProfileAuthController, ProfileAuthError

    safe_root = tmp_path / "safe"
    outside = tmp_path / "outside"
    safe_root.mkdir()
    outside.mkdir()
    redirect = safe_root / "redirect"
    redirect.symlink_to(outside, target_is_directory=True)
    controller = ProfileAuthController(redirect / "profiles")

    with pytest.raises(ProfileAuthError, match="profiles root.*symlink"):
        controller.ensure_profile_home("profile-codex-a", "codex")

    assert not (outside / "profiles").exists()


def test_status_timeout_terminates_cli_process_group_descendants(
    tmp_path, monkeypatch
):
    from cli_agent_service.auth import ProfileAuthController

    executable = _timeout_process_tree_cli(tmp_path)
    heartbeat = tmp_path / "child-heartbeat"
    child_pid_path = tmp_path / "child-pid"
    monkeypatch.setenv("PROFILE_AUTH_TEST_HEARTBEAT", str(heartbeat))
    monkeypatch.setenv("PROFILE_AUTH_TEST_CHILD_PID", str(child_pid_path))
    controller = ProfileAuthController(
        tmp_path / "profiles",
        codex_executable=str(executable),
        timeout_seconds=0.35,
    )
    controller.prepare_login("profile-codex-a", "codex")

    started_at = time.monotonic()
    try:
        result = controller.auth_status("profile-codex-a", "codex")
        elapsed = time.monotonic() - started_at

        assert result.state == "blocked"
        assert result.reason_code == "codex_status_probe_timed_out"
        assert elapsed < 1.5
        assert child_pid_path.is_file()
        assert heartbeat.is_file()
        time.sleep(0.1)
        stopped_value = heartbeat.read_text(encoding="utf-8")
        time.sleep(0.15)
        assert heartbeat.read_text(encoding="utf-8") == stopped_value
    finally:
        if child_pid_path.is_file():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_concurrent_state_writes_use_unique_atomic_temp_files(
    tmp_path, monkeypatch
):
    from cli_agent_service import auth as auth_module
    from cli_agent_service.auth import ProfileAuthController, ProfileAuthResult

    controller = ProfileAuthController(tmp_path / "profiles")
    home, _ = controller.ensure_profile_home("profile-codex-a", "codex")
    profile_dir = home.parent
    state_path = profile_dir / "auth-state.json"
    writer_count = 6
    replace_barrier = Barrier(writer_count)
    source_paths = []
    source_paths_lock = Lock()
    real_replace = os.replace

    def synchronized_replace(source, destination):
        with source_paths_lock:
            source_paths.append(str(source))
        replace_barrier.wait(timeout=5)
        real_replace(source, destination)

    monkeypatch.setattr(auth_module.os, "replace", synchronized_replace)
    results = [
        ProfileAuthResult(
            profile_id="profile-codex-a",
            provider="codex",
            state="discovered",
            reason_code="concurrent-write-{}".format(index),
        )
        for index in range(writer_count)
    ]

    def write_concurrent_state(result):
        controller._write_state(profile_dir, result)

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        list(executor.map(write_concurrent_state, results))

    assert len(source_paths) == writer_count
    assert len(set(source_paths)) == writer_count
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["reason_code"] in {result.reason_code for result in results}
    assert not list(profile_dir.glob(".auth-state.json.*.tmp"))


def test_nested_test_helpers_have_unique_overlay_symbols():
    syntax_tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    nested_names = [
        child.name
        for test_node in syntax_tree.body
        if isinstance(test_node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in test_node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    assert "runner" not in nested_names
    assert len(nested_names) == len(set(nested_names))
