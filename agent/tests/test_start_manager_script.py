from pathlib import Path


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


def test_start_manager_posix_script_bootstraps_service_manager_without_takeover():
    script = START_MANAGER_SH.read_text(encoding="utf-8")

    assert START_MANAGER_SH.exists()
    assert "agent/service_manager.py" in script
    assert "--health-wait-seconds" in script
    assert "Takeover is not supported" in script
    assert "subprocess.Popen" in script
    assert "start_new_session=True" in script
    assert "stdin=subprocess.DEVNULL" in script
    assert "agent/executor_worker.py.*--project" in script
    assert "MANAGER_URL" in script
