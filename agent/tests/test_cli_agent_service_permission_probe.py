import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cli-agent-permission-probe.py"


def _module():
    spec = importlib.util.spec_from_file_location("cli_agent_permission_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _executable(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n{}\n".format(body), encoding="utf-8")
    path.chmod(0o700)
    return path


def test_probe_classifies_supported_outcomes(tmp_path):
    probe = _module()
    allowed = _executable(tmp_path, "codex-ok", "echo codex-cli-1.0; exit 0")
    interactive = _executable(tmp_path, "claude-login", "echo 'Open browser to sign in to continue' >&2; exit 1")
    denied = _executable(tmp_path, "codex-denied", "echo 'permission denied' >&2; exit 1")
    failed = _executable(tmp_path, "claude-failed", "echo failed >&2; exit 2")

    assert probe.probe_executable("codex", executable=str(allowed))["status"] == "allowed"
    assert probe.probe_executable("claude", executable=str(interactive))["status"] == "requires_interaction"
    assert probe.probe_executable("codex", executable=str(denied))["status"] == "denied"
    assert probe.probe_executable("claude", executable=str(failed))["status"] == "error"
    unavailable = probe.probe_executable("codex-missing", executable=None)
    assert unavailable["status"] == "unavailable"
    assert unavailable["executable_found"] is False


def test_probe_timeout_and_output_privacy(tmp_path):
    probe = _module()
    slow = _executable(tmp_path, "slow-codex", "sleep 1; echo done")
    result = probe.probe_executable("codex", executable=str(slow), timeout_seconds=0.05)
    assert result["status"] == "timeout"
    assert result["raw_output_stored"] is False
    assert result["auth_state_changed"] is False
    assert result["output_hash"].startswith("sha256:")
    assert "stdout" not in result
    assert "stderr" not in result


def test_probe_cli_emits_capability_facts(tmp_path):
    codex = _executable(tmp_path, "codex", "echo codex-cli-1.0; exit 0")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--provider", "codex", "--codex-executable", str(codex)],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "cli_agent_service.permission_probe.v1"
    assert payload["results"][0]["provider"] == "codex"
    assert payload["results"][0]["status"] == "allowed"
    assert payload["results"][0]["raw_output_stored"] is False
