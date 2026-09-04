import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[2]
START_MANAGER = REPO_ROOT / "scripts" / "start-manager.ps1"
START_MANAGER_SH = REPO_ROOT / "scripts" / "start-manager.sh"


def _script_text() -> str:
    return START_MANAGER.read_text(encoding="utf-8")


def test_start_manager_worker_detection_matches_single_backslash_windows_paths():
    script = _script_text()

    assert '$cmd -like "*agent\\executor_worker.py*"' in script
    assert '$cmd -like "*agent\\\\executor_worker.py*"' not in script
    assert '$cmd -like "*agent\\mcp\\server.py*"' in script
    assert '$cmd -like "*agent\\\\mcp\\\\server.py*"' not in script


def test_start_manager_takeover_process_cleanup_is_best_effort():
    script = _script_text()

    assert "function Stop-ManagerProcessTree" in script
    assert 'Start-Process -FilePath "taskkill.exe"' in script
    assert "-ErrorAction SilentlyContinue" in script
    assert "taskkill /F /T /PID" not in script
    assert "Stop-ManagerProcessTree -TargetPid $pidVal" in script
    assert "Stop-ManagerProcessTree -TargetPid $id" in script


def test_start_manager_takeover_does_not_stop_mcp_without_explicit_flag():
    script = _script_text()

    assert "[switch]$StopMcp" in script
    assert "Takeover: leaving MCP server processes running" in script
    assert "Pass -StopMcp for explicit MCP cleanup" in script
    assert "if ($StopMcp) {" in script
    assert "Get-McpServerProcesses | Select-Object -ExpandProperty ProcessId -Unique" in script
    assert "Takeover: stopping existing MCP server PID=$id" in script


def test_start_manager_posix_script_bootstraps_service_manager_without_takeover():
    script = START_MANAGER_SH.read_text(encoding="utf-8")

    assert START_MANAGER_SH.exists()
    assert "agent/service_manager.py" in script
    assert "--health-wait-seconds" in script
    assert "Takeover is not supported" in script
    assert "subprocess.Popen" in script
    assert "start_new_session=True" in script
    assert "stdin=subprocess.DEVNULL" in script
    assert "wait_for_manager_health" in script
    assert "executor_state: waived_or_degraded" in script
    assert "Managed executor worker did not appear" not in script
    assert "Manager sidecar is healthy but executor worker did not appear" not in script
    assert "agent/executor_worker.py.*--project" in script
    assert "MANAGER_URL" in script


def _run_posix_start_manager(tmp_path: Path, *, mode: str, lock_held: bool = False):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    agent = repo / "agent"
    fake_bin = tmp_path / "fake-bin"
    scripts.mkdir(parents=True)
    agent.mkdir()
    fake_bin.mkdir()
    script_path = scripts / "start-manager.sh"
    script_path.write_text(START_MANAGER_SH.read_text(encoding="utf-8"), encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    (agent / "requirements.txt").write_text("", encoding="utf-8")
    (agent / "service_manager.py").write_text("", encoding="utf-8")

    fake_python = fake_bin / "python3"
    fake_python.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            mode = os.environ["FAKE_MANAGER_MODE"]
            state = Path(os.environ["FAKE_MANAGER_STATE"])
            count_path = Path(os.environ["FAKE_HEALTH_COUNT"])
            argv_path = Path(os.environ["FAKE_LAUNCH_ARGV"])

            if args and args[0] == "-" and len(args) == 2:
                count = int(count_path.read_text() or "0") if count_path.exists() else 0
                count += 1
                count_path.write_text(str(count))
                healthy = (
                    mode == "healthy"
                    or (mode == "launch_success" and state.exists())
                    or (mode == "lock_then_healthy" and count >= 2)
                )
                raise SystemExit(0 if healthy else 1)
            if args and args[0] == "-c":
                raise SystemExit(0)
            if args and args[0] == "-":
                argv_path.write_text(json.dumps(args[1:]))
                state.touch()
                print("4242")
                raise SystemExit(0)
            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    fake_pgrep = fake_bin / "pgrep"
    fake_pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_pgrep.chmod(fake_pgrep.stat().st_mode | stat.S_IXUSR)

    shared_volume = tmp_path / "shared"
    if lock_held:
        (shared_volume / "codex-tasks" / "state" / "manager-start.lock").mkdir(
            parents=True
        )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_MANAGER_MODE": mode,
        "FAKE_MANAGER_STATE": str(tmp_path / "manager-healthy"),
        "FAKE_HEALTH_COUNT": str(tmp_path / "health-count"),
        "FAKE_LAUNCH_ARGV": str(tmp_path / "launch-argv.json"),
        "SHARED_VOLUME_PATH": str(shared_volume),
        "MANAGER_URL": "http://manager.test",
        "CODEX_WORKSPACE": str(repo),
    }
    result = subprocess.run(
        [
            "bash",
            str(script_path),
            "--project",
            "ac-dev",
            "--health-wait-seconds",
            "1",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, tmp_path / "launch-argv.json"


def test_start_manager_posix_healthy_manager_succeeds_without_worker(tmp_path):
    result, launch_argv = _run_posix_start_manager(tmp_path, mode="healthy")

    assert result.returncode == 0
    assert "Manager already healthy." in result.stdout
    assert "executor_state: waived_or_degraded" in result.stdout
    assert "not_observed (optional)" in result.stdout
    assert not launch_argv.exists()


def test_start_manager_posix_launch_health_succeeds_without_worker_and_keeps_project(tmp_path):
    result, launch_argv = _run_posix_start_manager(tmp_path, mode="launch_success")

    assert result.returncode == 0
    assert "Manager healthy." in result.stdout
    assert "executor_state: waived_or_degraded" in result.stdout
    assert "ac-dev" in json.loads(launch_argv.read_text(encoding="utf-8"))


def test_start_manager_posix_manager_health_failure_is_manager_specific(tmp_path):
    result, _ = _run_posix_start_manager(tmp_path, mode="unhealthy")

    assert result.returncode == 1
    assert "ServiceManager health did not become healthy" in result.stderr
    assert "worker did not appear" not in result.stderr


def test_start_manager_posix_lock_contention_waits_for_health(tmp_path):
    result, launch_argv = _run_posix_start_manager(
        tmp_path,
        mode="lock_then_healthy",
        lock_held=True,
    )

    assert result.returncode == 0
    assert "another launcher held the lock" in result.stdout
    assert "executor_state: waived_or_degraded" in result.stdout
    assert not launch_argv.exists()


def test_start_manager_powershell_static_parity_contract_uses_manager_health():
    script = _script_text()

    assert "function Get-ManagerHealth" in script
    assert "function Wait-ManagerHealth" in script
    assert "/api/manager/health" in script
    assert "Invoke-RestMethod" in script
    assert "executor_state =" in script
    assert '"waived_or_degraded"' in script
    assert "Wait-ManagedWorker" not in script
    assert "Managed executor worker did not appear" not in script
    assert '"--project", $Project' in script
