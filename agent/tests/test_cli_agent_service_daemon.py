import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for {}".format(path))


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(AGENT_DIR)
    return env


def test_daemon_start_status_health_and_stop(tmp_path):
    from cli_agent_service.service import ServicePaths

    state_dir = tmp_path / "private-state"
    paths = ServicePaths.from_state_dir(state_dir)
    command = [
        sys.executable,
        "-m",
        "cli_agent_service",
        "start",
        "--state-dir",
        str(state_dir),
    ]
    process = subprocess.Popen(command, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(paths.socket_path)
        status = subprocess.run(
            [sys.executable, "-m", "cli_agent_service", "status", "--state-dir", str(state_dir)],
            env=_env(), check=False, capture_output=True, text=True, timeout=5,
        )
        payload = json.loads(status.stdout)
        assert status.returncode == 0
        assert payload["status"] == "running"
        assert payload["accepting_agent_runs"] is False
        assert payload["raw_credentials_exposed"] is False

        health = subprocess.run(
            [sys.executable, "-m", "cli_agent_service", "health", "--state-dir", str(state_dir)],
            env=_env(), check=False, capture_output=True, text=True, timeout=5,
        )
        assert json.loads(health.stdout)["ok"] is True
        assert os.stat(state_dir).st_mode & 0o777 == 0o700
        assert os.stat(paths.socket_path).st_mode & 0o777 == 0o600
        assert os.stat(state_dir / "status.json").st_mode & 0o777 == 0o600

        stopped = subprocess.run(
            [sys.executable, "-m", "cli_agent_service", "stop", "--state-dir", str(state_dir)],
            env=_env(), check=False, capture_output=True, text=True, timeout=5,
        )
        assert stopped.returncode == 0
        assert json.loads(stopped.stdout)["status"] == "stopping"
        assert process.wait(timeout=5) == 0
        assert json.loads((state_dir / "status.json").read_text())["status"] == "stopped"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_health_projection_is_deterministic_and_public_safe():
    from cli_agent_service.health import health_payload

    started = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    current = datetime(2026, 7, 12, 12, 0, 7, tzinfo=timezone.utc)
    payload = health_payload(pid=42, started_at=started, socket_ready=True, now=current)
    assert payload == {
        "schema_version": "cli_agent_service.health.v1",
        "service": "cli_agent_service",
        "ok": True,
        "status": "running",
        "pid": 42,
        "started_at": "2026-07-12T12:00:00.000000Z",
        "uptime_seconds": 7,
        "socket_ready": True,
        "accepting_agent_runs": False,
        "raw_credentials_exposed": False,
    }


def test_daemon_socket_rejects_caller_owned_desktop_authority(tmp_path):
    from cli_agent_service.service import ServicePaths, request_service

    state_dir = tmp_path / "private-state"
    paths = ServicePaths.from_state_dir(state_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_service",
            "start",
            "--state-dir",
            str(state_dir),
        ],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(paths.socket_path)
        response = request_service(
            paths,
            "desktop_execution_ticket_admit",
            payload={
                "host_kind": "codex_desktop",
                "project_id": "aming-claw",
                "backlog_id": "AC-FORGED",
                "contract_execution_id": "cex-forged",
                "runtime_context_id": "mfrctx-forged",
                "task_id": "task-forged",
                "worker_id": "worker-forged",
                "worker_slot_id": "slot-forged",
                "observer_command_id": "command-forged",
                "contract_runtime_current_state": {"source_of_authority": "ContractRuntime"},
                "execution_ticket": {"status": "issued", "issue_allowed": True},
            },
        )
        assert response["ok"] is False
        assert response["status"] == "invalid_request"
        assert "unsupported authority fields" in response["error"]
    finally:
        if process.poll() is None:
            try:
                request_service(paths, "stop")
            except Exception:
                process.terminate()
            process.wait(timeout=5)


def test_daemon_is_not_coupled_to_service_manager():
    for name in ("service.py", "health.py", "__main__.py"):
        source = (AGENT_DIR / "cli_agent_service" / name).read_text(encoding="utf-8")
        assert "service_manager" not in source.casefold()
        assert "ServiceManager" not in source


def test_macos_launch_agent_dry_run_contains_only_service_paths(tmp_path):
    script = REPO_ROOT / "scripts" / "install-cli-agent-service-macos.sh"
    completed = subprocess.run(
        [
            "sh", str(script), "--dry-run", "--python", sys.executable,
            "--repo-root", str(REPO_ROOT), "--state-dir", str(tmp_path / "state"),
        ],
        check=True, capture_output=True, text=True,
    )
    output = completed.stdout
    assert "dev.amingclaw.cli-agent-service" in output
    assert "<string>cli_agent_service</string>" in output
    assert "<string>start</string>" in output
    assert "ServiceManager" not in output
    assert "CODEX_HOME" not in output
    assert "CLAUDE_CONFIG_DIR" not in output
    assert "API_KEY" not in output
