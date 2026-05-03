from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = REPO_ROOT / "scripts" / "start-governance.ps1"


def _script_text() -> str:
    return START_SCRIPT.read_text(encoding="utf-8")


def test_start_governance_redirects_child_stdout_and_stderr_to_shared_logs():
    script = _script_text()

    assert 'Join-Path $env:SHARED_VOLUME_PATH "codex-tasks"' in script
    assert 'Join-Path (Join-Path $env:SHARED_VOLUME_PATH "codex-tasks") "logs"' in script
    assert "New-Item -ItemType Directory -Force -Path $logDir" in script
    assert '"governance-$Port-$logStamp.out.log"' in script
    assert '"governance-$Port-$logStamp.err.log"' in script
    assert "-RedirectStandardOutput $stdoutLog" in script
    assert "-RedirectStandardError $stderrLog" in script


def test_start_governance_exports_and_prints_log_paths_before_process_start():
    script = _script_text()
    start_process_index = script.index("Start-Process -FilePath $PYTHON")

    for required in (
        "$env:GOVERNANCE_STDOUT_LOG = $stdoutLog",
        "$env:GOVERNANCE_STDERR_LOG = $stderrLog",
        'Write-Host "  stdout:    $stdoutLog"',
        'Write-Host "  stderr:    $stderrLog"',
    ):
        assert required in script
        assert script.index(required) < start_process_index


def test_start_governance_takeover_uses_best_effort_process_tree_cleanup():
    script = _script_text()

    assert "function Stop-GovernanceProcessTree" in script
    assert 'Start-Process -FilePath "taskkill.exe"' in script
    assert "-ErrorAction SilentlyContinue" in script
    assert "taskkill /F /T /PID" not in script
    assert "Stop-GovernanceProcessTree -TargetPid $pidVal" in script
    assert "Stop-GovernanceProcessTree -TargetPid $procId" in script
