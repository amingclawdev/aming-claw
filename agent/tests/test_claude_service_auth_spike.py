from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "claude-service-auth-spike.py"
SPEC = importlib.util.spec_from_file_location("claude_service_auth_spike", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
spike = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, '{"loggedIn": true}', "", spike.AUTHENTICATED),
        (1, '{"loggedIn": false}', "", spike.UNAUTHENTICATED),
        (1, "", "not logged in", spike.UNAUTHENTICATED),
        (1, "", "errSecInteractionNotAllowed", spike.KEYCHAIN_OR_GUI),
        (1, "", "invalid config", spike.CONFIGURATION_MISMATCH),
        (1, "", "ordinary command failure", spike.PROVIDER_FAILURE),
        (0, "{}", "", spike.PROVIDER_FAILURE),
    ],
)
def test_classify_auth_status(returncode, stdout, stderr, expected):
    classification, _reason = spike.classify_auth_status(
        returncode, stdout, stderr
    )
    assert classification == expected


def test_timeout_is_conservatively_interactive_only():
    classification, reason = spike.classify_auth_status(
        None, timed_out=True
    )
    assert classification == spike.KEYCHAIN_OR_GUI
    assert reason == "noninteractive_status_timed_out"


@pytest.mark.parametrize(
    ("classifications", "expected_decision", "expected_reason"),
    [
        (
            [spike.AUTHENTICATED] * 3,
            "unattended-safe",
            "all_profiles_authenticated_noninteractively",
        ),
        (
            [spike.AUTHENTICATED, spike.UNAUTHENTICATED, spike.UNAUTHENTICATED],
            "interactive-only",
            "clean_profile_authentication_required",
        ),
        (
            [spike.AUTHENTICATED, spike.KEYCHAIN_OR_GUI, spike.AUTHENTICATED],
            "interactive-only",
            "keychain_acl_or_gui_prompt",
        ),
        (
            [spike.UNAUTHENTICATED, spike.UNAUTHENTICATED, spike.UNAUTHENTICATED],
            "reject",
            "inherited_profile_unauthenticated",
        ),
        (
            [spike.AUTHENTICATED, spike.CONFIGURATION_MISMATCH, spike.AUTHENTICATED],
            "reject",
            "configuration_mismatch",
        ),
        (
            [spike.AUTHENTICATED, spike.PROVIDER_FAILURE, spike.AUTHENTICATED],
            "reject",
            "provider_cli_failure",
        ),
    ],
)
def test_decision_matrix(classifications, expected_decision, expected_reason):
    results = [
        {"profile_id": profile_id, "classification": classification}
        for profile_id, classification in zip(
            spike.PROFILE_IDS, classifications, strict=True
        )
    ]
    assert spike.decide(results) == (expected_decision, expected_reason)


def test_decision_rejects_duplicate_profile_rows():
    results = [
        {"profile_id": "inherited", "classification": spike.AUTHENTICATED},
        {"profile_id": "clean-1", "classification": spike.AUTHENTICATED},
        {"profile_id": "clean-2", "classification": spike.AUTHENTICATED},
        {"profile_id": "clean-2", "classification": spike.AUTHENTICATED},
    ]

    assert spike.decide(results) == ("reject", "invalid_probe_set")


def test_probe_profile_never_returns_provider_output(tmp_path):
    def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true, "detail": "private-sentinel"}',
            stderr="",
        )

    result = spike.probe_profile(
        profile_id="clean-1",
        profile_kind="clean",
        executable="claude-test",
        environment={},
        config_dir=tmp_path / "clean-1",
        runner=runner,
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["classification"] == spike.AUTHENTICATED
    assert result["raw_output_persisted"] is False
    assert "private-sentinel" not in serialized
    assert result["output_hash"].startswith("sha256:")


def test_probe_profile_classifies_launch_failure_without_exception_text(tmp_path):
    def runner(_command, **_kwargs):
        raise OSError("private-sentinel")

    result = spike.probe_profile(
        profile_id="clean-1",
        profile_kind="clean",
        executable="claude-test",
        environment={},
        config_dir=tmp_path / "clean-1",
        runner=runner,
    )

    assert result["classification"] == spike.PROVIDER_FAILURE
    assert result["reason"] == "cli_launch_failed"
    assert "private-sentinel" not in json.dumps(result)


def test_probe_profile_classifies_timeout_without_partial_output(tmp_path):
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output="private-sentinel",
            stderr="",
        )

    result = spike.probe_profile(
        profile_id="clean-1",
        profile_kind="clean",
        executable="claude-test",
        environment={},
        config_dir=tmp_path / "clean-1",
        runner=runner,
    )

    assert result["classification"] == spike.KEYCHAIN_OR_GUI
    assert result["timed_out"] is True
    assert "private-sentinel" not in json.dumps(result)


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_probe_timeout_terminates_descendant_process_group(tmp_path):
    ready_path = tmp_path / "descendant-ready"
    child_program = "\n".join(
        (
            "import os",
            "import pathlib",
            "import signal",
            "import time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"pathlib.Path({str(ready_path)!r}).write_text(",
            "    f'{os.getpid()} {os.getpgrp()}', encoding='utf-8'",
            ")",
            "while True:",
            "    time.sleep(1)",
        )
    )
    provider = tmp_path / "claude-test"
    provider.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                f"ready_path = pathlib.Path({str(ready_path)!r})",
                f"child_program = {child_program!r}",
                "subprocess.Popen([sys.executable, '-c', child_program])",
                "deadline = time.monotonic() + 2",
                "while not ready_path.exists() and time.monotonic() < deadline:",
                "    time.sleep(0.01)",
                "print('provider-private-sentinel', flush=True)",
                "print('provider-private-sentinel', file=sys.stderr, flush=True)",
                "while True:",
                "    time.sleep(1)",
            )
        ),
        encoding="utf-8",
    )
    provider.chmod(0o700)

    result = spike.probe_profile(
        profile_id="inherited",
        profile_kind="inherited",
        executable=str(provider),
        environment={},
        timeout_seconds=0.75,
    )

    assert result["classification"] == spike.KEYCHAIN_OR_GUI
    assert result["timed_out"] is True
    assert "provider-private-sentinel" not in json.dumps(result)
    descendant_pid, process_group_id = map(
        int, ready_path.read_text(encoding="utf-8").split()
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("timed-out provider descendant is still running")
    assert not spike._process_group_exists(process_group_id)


def test_run_spike_uses_inherited_and_two_distinct_empty_profiles():
    calls = []
    clean_paths = []

    def runner(command, **kwargs):
        env = kwargs["env"]
        calls.append((command, kwargs))
        config_dir = env.get("CLAUDE_CONFIG_DIR")
        if config_dir != "/inherited-profile":
            path = Path(config_dir)
            clean_paths.append(path)
            assert path.is_dir()
            assert list(path.iterdir()) == []
        return SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true}',
            stderr="",
        )

    report = spike.run_spike(
        executable="claude-test",
        environment={
            "CLAUDE_CONFIG_DIR": "/inherited-profile",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
        },
        runner=runner,
    )

    assert report["decision"] == "unattended-safe"
    assert [result["profile_id"] for result in report["profiles"]] == list(
        spike.PROFILE_IDS
    )
    assert len(calls) == 3
    assert len(set(clean_paths)) == 2
    assert all(not path.exists() for path in clean_paths)
    for command, kwargs in calls:
        assert tuple(command[1:]) == spike.AUTH_STATUS_ARGS
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]


def test_anthropic_auth_token_is_scrubbed_from_all_profile_environments():
    token_presence = []

    def runner(_command, **kwargs):
        token_present = "ANTHROPIC_AUTH_TOKEN" in kwargs["env"]
        token_presence.append(token_present)
        return SimpleNamespace(
            returncode=0 if token_present else 1,
            stdout=(
                '{"loggedIn": true}'
                if token_present
                else '{"loggedIn": false}'
            ),
            stderr="",
        )

    report = spike.run_spike(
        executable="claude-test",
        environment={"ANTHROPIC_AUTH_TOKEN": ""},
        runner=runner,
    )

    assert token_presence == [False, False, False]
    assert report["decision"] == "reject"
    assert report["decision"] != "unattended-safe"
    assert {
        result["classification"] for result in report["profiles"]
    } == {spike.UNAUTHENTICATED}


def test_missing_cli_rejects_with_public_safe_results(monkeypatch):
    monkeypatch.setattr(spike.shutil, "which", lambda _candidate: None)

    report = spike.run_spike(environment={})

    assert report["decision"] == "reject"
    assert report["decision_reason"] == "provider_cli_failure"
    assert {item["reason"] for item in report["profiles"]} == {"cli_unavailable"}
    assert report["probe_contract"] == {
        "profile_count": 3,
        "clean_profile_count": 2,
        "authentication_material_copied": False,
        "direct_auth_environment_forwarded": False,
        "raw_credentials_exposed": False,
        "raw_prompt_used": False,
        "raw_output_persisted": False,
    }


def test_claude_bin_environment_override_is_used():
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true}',
            stderr="",
        )

    report = spike.run_spike(
        environment={"CLAUDE_BIN": "/host/claude"},
        runner=runner,
    )

    assert report["decision"] == "unattended-safe"
    assert [command[0] for command in commands] == ["/host/claude"] * 3


def test_status_command_is_prompt_free():
    assert spike.AUTH_STATUS_ARGS == ("auth", "status", "--json")
    assert all("prompt" not in argument.casefold() for argument in spike.AUTH_STATUS_ARGS)
